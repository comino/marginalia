"""Tests for the live change stream (fake socket + fake metadata)."""

import asyncio
import json

from remarkable_mcp.workflows import live
from remarkable_mcp.workflows.live import DocState, diff


def _doc(name, pages, parent="", trashed=False):
    return DocState(name=name, path="/" + name, parent=parent, pages=pages, trashed=trashed)


def test_diff_reports_changed_pages_new_moved_removed():
    old = {
        "a": _doc("A", {"a/p1.rm": "h1", "a/p2.rm": "h2"}),
        "b": _doc("B", {}),
        "c": _doc("C", {}),
        "d": _doc("D", {}),
    }
    new = {
        "a": _doc("A", {"a/p1.rm": "h1", "a/p2.rm": "CHANGED", "a/p3.rm": "h3"}),
        "b": _doc("B", {}, parent="folder"),
        "c": _doc("C", {}, trashed=True),
        "e": _doc("E", {"e/x.rm": "1"}),
    }
    changes = {c.doc_id: c for c in diff(old, new)}
    assert changes["a"].kind == "ink" and changes["a"].pages == ["p2", "p3"]
    assert changes["b"].kind == "moved"
    assert changes["c"].kind == "removed"
    assert changes["d"].kind == "removed"
    assert changes["e"].kind == "new"
    assert diff(new, new) == []


class FakeSocket:
    def __init__(self, messages):
        self.messages = list(messages)

    def __aiter__(self):
        return self

    async def __anext__(self):
        if not self.messages:
            await asyncio.sleep(3600)
        await asyncio.sleep(0.01)
        return self.messages.pop(0)

    async def close(self):
        pass


def test_watcher_turns_notifications_into_changes(monkeypatch):
    monkeypatch.setattr(live.asyncio, "sleep", _fast_sleep)
    states = [
        {"a": _doc("Sketch", {"a/p1.rm": "1"})},
        {"a": _doc("Sketch", {"a/p1.rm": "2"})},
    ]
    calls = {"n": 0}

    def snap():
        i = min(calls["n"], len(states) - 1)
        calls["n"] += 1
        return states[i]

    sync = json.dumps({"message": {"attributes": {"event": "SyncComplete", "sourceDeviceID": "x"}}})
    other = json.dumps({"message": {"attributes": {"event": "ScreenShareStarted"}}})

    async def connect():
        return FakeSocket([other, sync])

    async def scenario():
        w = live.Watcher(take_snapshot=snap, connect=connect)
        q = w.subscribe()
        task = asyncio.create_task(w.run())
        ch = await asyncio.wait_for(q.get(), timeout=5)
        task.cancel()
        return w, ch

    w, ch = asyncio.run(scenario())
    assert ch.doc_id == "a" and ch.pages == ["p1"] and ch.kind == "ink"
    assert w.mode == "socket" and w.events_seen == 1


def test_watcher_falls_back_to_polling(monkeypatch):
    monkeypatch.setattr(live.asyncio, "sleep", _fast_sleep)
    states = iter([{"a": _doc("N", {})}] + [{"a": _doc("N", {"a/p.rm": "1"})}] * 50)

    async def connect():
        raise OSError("no socket here")

    async def scenario():
        w = live.Watcher(take_snapshot=lambda: next(states), connect=connect, poll_seconds=0.01)
        q = w.subscribe()
        task = asyncio.create_task(w.run())
        ch = await asyncio.wait_for(q.get(), timeout=5)
        task.cancel()
        return w, ch

    w, ch = asyncio.run(scenario())
    assert w.mode == "polling" and ch.kind == "ink"


_real_sleep = asyncio.sleep


async def _fast_sleep(seconds, *a, **k):
    await _real_sleep(min(seconds, 0.01))


def test_live_watch_tool_times_out_cleanly(monkeypatch):
    from remarkable_mcp.workflows import live_tools

    class Idle:
        mode, events_seen = "socket", 0

        def subscribe(self):
            return asyncio.Queue()

        def unsubscribe(self, q):
            pass

    async def fake_shared():
        return Idle()

    monkeypatch.setattr(live, "shared_watcher", fake_shared)
    out = json.loads(asyncio.run(live_tools.remarkable_live_watch(timeout=5)))
    assert out["status"] == "no_change"


def test_live_watch_tool_batches_and_filters(monkeypatch):
    from remarkable_mcp.workflows import live_tools

    q: asyncio.Queue = asyncio.Queue()

    class Busy:
        mode, events_seen = "socket", 3

        def subscribe(self):
            return q

        def unsubscribe(self, q):
            pass

    async def fake_shared():
        for ch in (
            live.Change("x", "Other", "/Other", "ink", ["p9"]),
            live.Change("a", "Sketch", "/Sketch", "ink", ["p1"]),
            live.Change("a", "Sketch", "/Sketch", "ink", ["p2"]),
        ):
            q.put_nowait(ch)
        return Busy()

    monkeypatch.setattr(live, "shared_watcher", fake_shared)
    out = json.loads(
        asyncio.run(
            live_tools.remarkable_live_watch("sketch", timeout=5, settle=0.2, analyse="none")
        )
    )
    assert out["status"] == "changed"
    [change] = out["changes"]
    assert change["document"] == "Sketch" and change["changed_page_ids"] == ["p1", "p2"]
