"""Failure modes of the live watcher: it must never die, and must not hammer the API."""

import asyncio
import json

import pytest

from remarkable_mcp.workflows import live
from remarkable_mcp.workflows.live import DocState

SYNC = json.dumps({"message": {"attributes": {"event": "SyncComplete", "sourceDeviceID": "t"}}})
_real_sleep = asyncio.sleep


async def _fast_sleep(seconds, *a, **k):
    await _real_sleep(min(seconds, 0.005))


@pytest.fixture(autouse=True)
def fast(monkeypatch):
    monkeypatch.setattr(live.asyncio, "sleep", _fast_sleep)
    monkeypatch.setattr(live, "MIN_REFRESH_SECONDS", 0.05)


def _doc(h):
    return {"a": DocState(name="A", path="/A", parent="", pages={"a/p.rm": h}, trashed=False)}


class Socket:
    """Yields the given messages, then either ends (server drop) or idles."""

    def __init__(self, messages, then="drop"):
        self.messages, self.then = list(messages), then

    def __aiter__(self):
        return self

    async def __anext__(self):
        if self.messages:
            await _real_sleep(0.001)
            return self.messages.pop(0)
        if self.then == "drop":
            raise StopAsyncIteration
        if self.then == "error":
            raise ConnectionResetError("reset by peer")
        await _real_sleep(3600)

    async def close(self):
        pass


def run_until(watcher, predicate, timeout=5.0):
    async def go():
        task = asyncio.create_task(watcher.run())
        try:
            end = asyncio.get_running_loop().time() + timeout
            while not predicate():
                if task.done():
                    raise AssertionError(f"watcher died: {task.exception()!r}")
                if asyncio.get_running_loop().time() > end:
                    raise AssertionError("timed out")
                await _real_sleep(0.01)
        finally:
            task.cancel()

    asyncio.run(go())


def test_snapshot_errors_do_not_kill_the_watcher():
    calls = {"n": 0}

    def snap():
        calls["n"] += 1
        if calls["n"] in (1, 2, 3):
            raise RuntimeError("HTTP 429 Too Many Requests")
        return _doc(str(calls["n"]))

    async def connect():
        return Socket([SYNC, SYNC, SYNC], then="drop")

    w = live.Watcher(take_snapshot=snap, connect=connect)
    run_until(w, lambda: w.state is not None and calls["n"] >= 6)
    assert w.errors_total >= 3 and w.errors == 0


def test_bursts_are_coalesced():
    calls = {"n": 0}

    def snap():
        calls["n"] += 1
        return _doc(str(calls["n"]))

    async def connect():
        return Socket([SYNC] * 40, then="idle")

    w = live.Watcher(take_snapshot=snap, connect=connect)
    run_until(w, lambda: w.events_seen == 40)
    run_until(w, lambda: True)  # let trailing refreshes settle
    # 40 notifications in quick succession must not cause 40 metadata reads.
    assert calls["n"] <= 12


def test_socket_errors_reconnect_and_catch_up():
    calls = {"n": 0}
    connects = {"n": 0}

    def snap():
        calls["n"] += 1
        return _doc(str(calls["n"]))

    async def connect():
        connects["n"] += 1
        return Socket([SYNC], then="error")

    w = live.Watcher(take_snapshot=snap, connect=connect)
    q = w.subscribe()
    run_until(w, lambda: connects["n"] >= 3 and q.qsize() >= 2)


def test_unavailable_socket_backs_off_and_polls():
    connects = {"n": 0}

    async def connect():
        connects["n"] += 1
        raise OSError("network unreachable")

    w = live.Watcher(take_snapshot=lambda: _doc("x"), connect=connect, poll_seconds=0.01)
    run_until(w, lambda: connects["n"] >= 3)
    assert w.mode == "polling"


def test_slow_subscribers_do_not_grow_without_bound():
    n = {"i": 0}

    def snap():
        n["i"] += 1
        return _doc(str(n["i"]))

    w = live.Watcher(take_snapshot=snap, connect=None)
    q = w.subscribe()

    async def go():
        for _ in range(live.MAX_QUEUE + 50):
            await w.refresh()

    asyncio.run(go())
    assert q.qsize() <= live.MAX_QUEUE


def test_token_renewed_on_401(monkeypatch):
    from websockets.exceptions import InvalidStatus

    class Resp:
        status_code = 401

    attempts = {"n": 0}

    async def fake_connect(url, **kw):
        attempts["n"] += 1
        if attempts["n"] == 1:
            raise InvalidStatus(Resp())
        return Socket([], then="idle")

    class Client:
        user_token = "old"

        def renew_token(self):
            self.user_token = "new"

    client = Client()
    monkeypatch.setattr(live.cloud, "client", lambda: client)
    monkeypatch.setattr("websockets.connect", fake_connect)
    sock = asyncio.run(live.Watcher._default_connect())
    assert isinstance(sock, Socket) and client.user_token == "new" and attempts["n"] == 2
