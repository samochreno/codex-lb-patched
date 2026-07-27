#!/usr/bin/env python3
from __future__ import annotations

import argparse
import base64
import json
import os
import selectors
import subprocess
import sys
import time
from pathlib import Path
from typing import Any


def _send(process: subprocess.Popen[str], message: dict[str, Any]) -> None:
    assert process.stdin is not None
    process.stdin.write(json.dumps(message, separators=(",", ":")) + "\n")
    process.stdin.flush()


def _read_message(
    process: subprocess.Popen[str],
    selector: selectors.BaseSelector,
    *,
    deadline: float,
) -> dict[str, Any]:
    assert process.stdout is not None
    while time.monotonic() < deadline:
        events = selector.select(max(0.0, deadline - time.monotonic()))
        if not events:
            break
        line = process.stdout.readline()
        if not line:
            raise RuntimeError(f"Codex app-server exited with status {process.poll()}")
        payload = json.loads(line)
        if isinstance(payload, dict):
            return payload
    raise TimeoutError("Timed out waiting for Codex app-server")


def _wait_for_response(
    process: subprocess.Popen[str],
    selector: selectors.BaseSelector,
    request_id: int,
    *,
    timeout: float,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    deadline = time.monotonic() + timeout
    notifications: list[dict[str, Any]] = []
    while True:
        payload = _read_message(process, selector, deadline=deadline)
        if payload.get("id") == request_id:
            if "error" in payload:
                raise RuntimeError(f"App-server request {request_id} failed: {payload['error']}")
            return payload, notifications
        notifications.append(payload)


def main() -> int:
    parser = argparse.ArgumentParser(description="Smoke-test Codex Realtime sideband through a configured provider")
    parser.add_argument("--codex", default="/Applications/ChatGPT.app/Contents/Resources/codex")
    parser.add_argument("--cwd", default=os.getcwd())
    parser.add_argument("--thread-id")
    parser.add_argument("--thread-path")
    parser.add_argument("--webrtc-sdp-base64")
    parser.add_argument("--timeout", type=float, default=20.0)
    args = parser.parse_args()

    process = subprocess.Popen(
        [args.codex, "app-server", "--stdio", "--enable", "realtime_conversation"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        bufsize=1,
    )
    assert process.stdout is not None
    selector = selectors.DefaultSelector()
    selector.register(process.stdout, selectors.EVENT_READ)
    try:
        transport = {"type": "websocket"}
        if args.webrtc_sdp_base64:
            transport = {
                "type": "webrtc",
                "sdp": base64.b64decode(args.webrtc_sdp_base64).decode("utf-8"),
            }
        _send(
            process,
            {
                "id": 1,
                "method": "initialize",
                "params": {
                    "clientInfo": {
                        "name": "codex-lb-realtime-smoke",
                        "title": "CodexLB Realtime smoke test",
                        "version": "1",
                    },
                    "capabilities": {"experimentalApi": True},
                },
            },
        )
        _wait_for_response(process, selector, 1, timeout=args.timeout)
        _send(process, {"method": "initialized", "params": {}})

        if args.thread_id and args.thread_path:
            thread_method = "thread/resume"
            thread_params = {
                "threadId": args.thread_id,
                "path": str(Path(args.thread_path).resolve()),
            }
        else:
            thread_method = "thread/start"
            thread_params = {
                "cwd": str(Path(args.cwd).resolve()),
                "ephemeral": True,
                "modelProvider": "codex-lb",
                "threadSource": "realtime_voice",
            }
        _send(process, {"id": 2, "method": thread_method, "params": thread_params})
        thread_response, _ = _wait_for_response(process, selector, 2, timeout=args.timeout)
        thread_id = thread_response["result"]["thread"]["id"]

        _send(
            process,
            {
                "id": 3,
                "method": "thread/realtime/start",
                "params": {
                    "threadId": thread_id,
                    "outputModality": "audio",
                    "transport": transport,
                },
            },
        )
        _, notifications = _wait_for_response(process, selector, 3, timeout=args.timeout)
        deadline = time.monotonic() + args.timeout
        started_session_id: str | None = None
        while time.monotonic() < deadline:
            payload = notifications.pop(0) if notifications else _read_message(process, selector, deadline=deadline)
            method = payload.get("method")
            params = payload.get("params") if isinstance(payload.get("params"), dict) else {}
            if method == "thread/realtime/error":
                raise RuntimeError(f"Realtime session failed: {params.get('message') or params}")
            if method == "thread/realtime/started":
                value = params.get("realtimeSessionId")
                if isinstance(value, str) and value:
                    started_session_id = value
                    break

        if started_session_id is None:
            raise TimeoutError("Realtime session did not report an upstream session ID")

        grace_deadline = time.monotonic() + 3.0
        while time.monotonic() < grace_deadline:
            try:
                payload = _read_message(process, selector, deadline=grace_deadline)
            except TimeoutError:
                break
            if payload.get("method") == "thread/realtime/error":
                params = payload.get("params")
                raise RuntimeError(f"Realtime session failed after startup: {params}")

        _send(
            process,
            {
                "id": 4,
                "method": "thread/realtime/stop",
                "params": {"threadId": thread_id},
            },
        )
        _wait_for_response(process, selector, 4, timeout=args.timeout)
        print(f"REALTIME_SIDEBAND_OK session_prefix={started_session_id.split('_', 2)[0]}_")
        return 0
    finally:
        selector.close()
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)


if __name__ == "__main__":
    sys.exit(main())
