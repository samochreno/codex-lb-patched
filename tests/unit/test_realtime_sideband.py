from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

import app.modules.proxy._service.realtime_sideband as realtime_sideband_module
from app.modules.proxy._service.realtime_sideband import _RealtimeSidebandMixin

pytestmark = pytest.mark.unit


class _FakeSessionContext:
    async def __aenter__(self):
        return SimpleNamespace()

    async def __aexit__(self, exc_type, exc, traceback):
        return None


class _FakeAccountsRepository:
    def __init__(self, session):
        del session

    async def get_by_id(self, account_id: str):
        assert account_id == "local-account-1"
        return SimpleNamespace(
            access_token_encrypted=b"encrypted-token",
            chatgpt_account_id="upstream-account-1",
            codex_installation_id="installation-1",
        )


class _FakeEncryptor:
    def decrypt(self, value: bytes) -> str:
        assert value == b"encrypted-token"
        return "access-token"


class _FakeUpstream:
    def __init__(self) -> None:
        self.sent_bytes: list[bytes] = []
        self.closed = False
        self._received_input = asyncio.Event()
        self._sent_echo = False

    async def send_text(self, text: str) -> None:
        raise AssertionError(f"unexpected text frame: {text}")

    async def send_bytes(self, data: bytes) -> None:
        self.sent_bytes.append(data)
        self._received_input.set()

    async def receive(self):
        await self._received_input.wait()
        if not self._sent_echo:
            self._sent_echo = True
            return SimpleNamespace(kind="binary", data=b"audio-out", text=None, close_code=None)
        return SimpleNamespace(kind="close", data=None, text=None, close_code=1000)

    async def close(self) -> None:
        self.closed = True


class _FakeDownstream:
    def __init__(self) -> None:
        self.accepted = False
        self.sent_bytes: list[bytes] = []
        self.close_code: int | None = None
        self._messages = asyncio.Queue()
        self._messages.put_nowait({"type": "websocket.receive", "bytes": b"audio-in"})

    async def accept(self) -> None:
        self.accepted = True

    async def receive(self):
        return await self._messages.get()

    async def send_text(self, text: str) -> None:
        raise AssertionError(f"unexpected text frame: {text}")

    async def send_bytes(self, data: bytes) -> None:
        self.sent_bytes.append(data)

    async def close(self, code: int = 1000) -> None:
        self.close_code = code

    async def send_denial_response(self, response) -> None:
        raise AssertionError(f"unexpected denial: {response.status_code}")


class _FakeProxy(_RealtimeSidebandMixin):
    def __init__(self) -> None:
        self._encryptor = _FakeEncryptor()
        self._realtime_sideband_pins = {}
        self._realtime_sideband_lock = asyncio.Lock()

    async def _resolve_upstream_route_for_account(self, account, *, operation: str):
        del account
        assert operation == "realtime_sideband_websocket"
        return None


@pytest.mark.asyncio
async def test_realtime_sideband_relays_binary_frames_on_the_pinned_account(monkeypatch):
    proxy = _FakeProxy()
    upstream = _FakeUpstream()
    downstream = _FakeDownstream()
    connection: dict[str, object] = {}

    async def fake_connect(session_id, headers, access_token, account_id, **kwargs):
        connection.update(
            session_id=session_id,
            headers=headers,
            access_token=access_token,
            account_id=account_id,
            kwargs=kwargs,
        )
        return upstream

    monkeypatch.setattr(realtime_sideband_module, "SessionLocal", _FakeSessionContext)
    monkeypatch.setattr(realtime_sideband_module, "AccountsRepository", _FakeAccountsRepository)
    monkeypatch.setattr(realtime_sideband_module, "connect_realtime_sideband_websocket", fake_connect)

    assert await proxy.register_realtime_sideband("rtc_u2_test", "local-account-1")
    await proxy.proxy_realtime_sideband_websocket(
        downstream,
        "rtc_u2_test",
        {"authorization": "Bearer downstream-key", "user-agent": "Codex CLI Test"},
    )

    assert downstream.accepted
    assert upstream.sent_bytes == [b"audio-in"]
    assert downstream.sent_bytes == [b"audio-out"]
    assert downstream.close_code == 1000
    assert upstream.closed
    assert connection["session_id"] == "rtc_u2_test"
    assert connection["access_token"] == "access-token"
    assert connection["account_id"] == "upstream-account-1"
    assert connection["kwargs"] == {"route": None, "allow_direct_egress": True}
    assert await proxy._get_realtime_sideband_pin("rtc_u2_test") is None
