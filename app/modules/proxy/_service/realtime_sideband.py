from __future__ import annotations

import asyncio
import contextlib
import logging
import re
import time
from dataclasses import dataclass
from typing import Any, Mapping, cast

from fastapi import WebSocket
from fastapi.responses import JSONResponse

from app.core.clients.proxy import ProxyResponseError, apply_codex_installation_headers
from app.core.clients.proxy_websocket import (
    UpstreamResponsesWebSocket,
    connect_realtime_sideband_websocket,
)
from app.core.errors import openai_error
from app.core.upstream_proxy import UpstreamProxyRouteError
from app.db.session import SessionLocal
from app.modules.accounts.repository import AccountsRepository
from app.modules.proxy.helpers import _header_account_id

_REALTIME_SESSION_ID_PATTERN = re.compile(r"^rtc_[A-Za-z0-9_-]+$")
_REALTIME_SIDEBAND_PIN_TTL_SECONDS = 300.0
logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class _RealtimeSidebandPin:
    account_id: str
    expires_at: float


class _RealtimeSidebandMixin:
    async def register_realtime_sideband(self, session_id: str, account_id: str) -> bool:
        if _REALTIME_SESSION_ID_PATTERN.fullmatch(session_id) is None:
            return False
        proxy = cast(Any, self)
        now = time.monotonic()
        async with proxy._realtime_sideband_lock:
            proxy._realtime_sideband_pins = {
                key: value for key, value in proxy._realtime_sideband_pins.items() if value.expires_at > now
            }
            proxy._realtime_sideband_pins[session_id] = _RealtimeSidebandPin(
                account_id=account_id,
                expires_at=now + _REALTIME_SIDEBAND_PIN_TTL_SECONDS,
            )
        return True

    async def proxy_realtime_sideband_websocket(
        self,
        websocket: WebSocket,
        session_id: str,
        headers: Mapping[str, str],
    ) -> None:
        proxy = cast(Any, self)
        pin = await proxy._get_realtime_sideband_pin(session_id)
        if pin is None:
            await websocket.send_denial_response(
                JSONResponse(
                    status_code=404,
                    content={"error": {"code": "realtime_session_not_found", "message": "Realtime session not found"}},
                )
            )
            return

        async with SessionLocal() as session:
            account = await AccountsRepository(session).get_by_id(pin.account_id)
        if account is None:
            await websocket.send_denial_response(
                JSONResponse(
                    status_code=503,
                    content={"error": {"code": "account_unavailable", "message": "Pinned account is unavailable"}},
                )
            )
            return

        access_token = proxy._encryptor.decrypt(account.access_token_encrypted)
        forwarded_headers = apply_codex_installation_headers(
            dict(headers),
            getattr(account, "codex_installation_id", None),
        )
        try:
            route = await proxy._resolve_upstream_route_for_account(
                account,
                operation="realtime_sideband_websocket",
            )
            upstream = await connect_realtime_sideband_websocket(
                session_id,
                forwarded_headers,
                access_token,
                _header_account_id(account.chatgpt_account_id),
                route=route,
                allow_direct_egress=route is None,
            )
        except UpstreamProxyRouteError as exc:
            error = openai_error(
                "upstream_proxy_unavailable",
                f"Unable to resolve upstream proxy route: {exc.reason}",
                error_type="server_error",
            )
            await websocket.send_denial_response(JSONResponse(status_code=502, content=error))
            return
        except ProxyResponseError as exc:
            error = exc.payload.get("error") if isinstance(exc.payload, dict) else None
            error_code = error.get("code") if isinstance(error, dict) else None
            error_message = error.get("message") if isinstance(error, dict) else None
            logger.warning(
                "Realtime sideband upstream handshake rejected session_id=%s status=%s code=%s message=%s",
                session_id,
                exc.status_code,
                error_code,
                error_message,
            )
            await websocket.send_denial_response(JSONResponse(status_code=exc.status_code, content=exc.payload))
            return

        await websocket.accept()
        try:
            downstream_task = asyncio.create_task(_relay_downstream_to_upstream(websocket, upstream))
            upstream_task = asyncio.create_task(_relay_upstream_to_downstream(upstream, websocket))
            done, pending = await asyncio.wait(
                {downstream_task, upstream_task},
                return_when=asyncio.FIRST_COMPLETED,
            )
            for task in pending:
                task.cancel()
            for task in pending:
                with contextlib.suppress(asyncio.CancelledError):
                    await task
            for task in done:
                task.result()
        finally:
            with contextlib.suppress(Exception):
                await upstream.close()
            await proxy._remove_realtime_sideband_pin(session_id, pin)

    async def _get_realtime_sideband_pin(self, session_id: str) -> _RealtimeSidebandPin | None:
        proxy = cast(Any, self)
        now = time.monotonic()
        async with proxy._realtime_sideband_lock:
            pin = proxy._realtime_sideband_pins.get(session_id)
            if pin is None:
                return None
            if pin.expires_at <= now:
                proxy._realtime_sideband_pins.pop(session_id, None)
                return None
            return pin

    async def _remove_realtime_sideband_pin(
        self,
        session_id: str,
        expected: _RealtimeSidebandPin,
    ) -> None:
        proxy = cast(Any, self)
        async with proxy._realtime_sideband_lock:
            if proxy._realtime_sideband_pins.get(session_id) == expected:
                proxy._realtime_sideband_pins.pop(session_id, None)


async def _relay_downstream_to_upstream(
    downstream: WebSocket,
    upstream: UpstreamResponsesWebSocket,
) -> None:
    while True:
        message = await downstream.receive()
        message_type = message.get("type")
        if message_type == "websocket.disconnect":
            return
        text = message.get("text")
        if isinstance(text, str):
            await upstream.send_text(text)
            continue
        data = message.get("bytes")
        if isinstance(data, bytes):
            await upstream.send_bytes(data)


async def _relay_upstream_to_downstream(
    upstream: UpstreamResponsesWebSocket,
    downstream: WebSocket,
) -> None:
    while True:
        message = await upstream.receive()
        if message.kind == "text" and message.text is not None:
            await downstream.send_text(message.text)
            continue
        if message.kind == "binary" and message.data is not None:
            await downstream.send_bytes(message.data)
            continue
        close_code = message.close_code if message.close_code and 1000 <= message.close_code <= 4999 else 1000
        await downstream.close(code=close_code)
        return
