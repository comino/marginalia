"""Failure modes of the live watcher: it must never die, and must not hammer the API."""

import asyncio
import json

import pytest

from remarkable_mcp.workflows import live
from remarkable_mcp.workflows.live import DocState

SYNC = json.dumps({"message": {"attributes": {"event": "SyncComplete", "sourceDeviceID": "t"}}})
_real_sleep = asyncio.sleep


REQUESTED_SLEEPS = []


async def _fast_sleep(seconds, *a, **k):
    REQUESTED_SLEEPS.append(seconds)
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


def test_last_change_of_a_burst_is_never_lost():
    """A sync that lands while a refresh is running triggers one more refresh."""
    import time as _time

    versions = iter(["a", "b", "c", "c", "c", "c", "c", "c"])

    def snap():
        _time.sleep(0.05)  # a slow metadata read
        return _doc(next(versions))

    class Timed(Socket):
        async def __anext__(self):
            if self.messages:
                delay, msg = self.messages.pop(0)
                await _real_sleep(delay)
                return msg
            await _real_sleep(3600)

    async def connect():
        # the second sync arrives while the first refresh is still reading
        return Timed([(0.0, SYNC), (0.07, SYNC)])

    w = live.Watcher(take_snapshot=snap, connect=connect)
    run_until(w, lambda: w.state is not None and w.state["a"].pages["a/p.rm"] == "c")


def test_immediately_closed_sockets_back_off():
    connects = {"n": 0}

    async def connect():
        connects["n"] += 1
        return Socket([], then="drop")  # accepted, then closed at once

    w = live.Watcher(take_snapshot=lambda: _doc("x"), connect=connect)
    REQUESTED_SLEEPS.clear()
    run_until(w, lambda: connects["n"] >= 6)
    waits = [s for s in REQUESTED_SLEEPS if s >= 2.0]
    # Each quick drop doubles the wait before reconnecting: 4, 8, 16, ...
    assert waits[:4] == sorted(waits[:4]) and waits[3] >= 16


def test_new_documents_report_page_ids():
    from remarkable_mcp.workflows.live import diff

    new = {"d": DocState("N", "/N", "", {"d/pg1.rm": "1", "d/pg2.rm": "2"}, False)}
    [ch] = diff({}, new)
    assert ch.kind == "new" and ch.pages == ["pg1", "pg2"]


def test_a_document_missing_once_is_not_removed():
    snaps = iter(
        [
            _doc("1"),
            {},  # the client failed to load it this time
            _doc("1"),
            {},
            {},  # really gone
        ]
    )
    w = live.Watcher(take_snapshot=lambda: next(snaps), connect=None)

    async def go():
        out = []
        for _ in range(5):
            out += await w.refresh()
        return out

    changes = asyncio.run(go())
    assert [c.kind for c in changes] == ["removed"]


def test_changes_since_lets_consumers_resume():
    n = {"i": 0}

    def snap():
        n["i"] += 1
        return _doc(str(n["i"]))

    w = live.Watcher(take_snapshot=snap, connect=None)

    async def go():
        for _ in range(4):
            await w.refresh()

    asyncio.run(go())
    assert w.seq == 3
    assert [s for s, _ in w.changes_since(1)] == [2, 3]
    assert w.changes_since(3) == []
