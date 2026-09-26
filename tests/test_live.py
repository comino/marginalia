"""Tests for the live change stream (fake socket + fake metadata)."""

import asyncio
import json

from remarkable_mcp.workflows.live import watcher as live
from remarkable_mcp.workflows.live.watcher import DocState, diff


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


class FakeWatcher:
    """Just the parts of Watcher that remarkable_live_watch uses."""

    def __init__(self, changes=(), mode="socket"):
        self.mode, self.events_seen = mode, len(changes)
        self.history = [(n, ch) for n, ch in enumerate(changes, start=1)]
        self.seq = 0  # the tool snapshots seq at start; events "arrive" after

    def changes_since(self, seq):
        return [(n, ch) for n, ch in self.history if n > seq and n <= self.seq]

    def arrive(self, upto):
        self.seq = upto


def _patch(monkeypatch, watcher, arrivals=()):
    async def fake_shared():
        async def feed():
            for delay, upto in arrivals:
                await asyncio.sleep(delay)
                watcher.arrive(upto)

        asyncio.get_running_loop().create_task(feed())
        return watcher

    monkeypatch.setattr(live, "shared_watcher", fake_shared)


def test_live_watch_tool_times_out_cleanly(monkeypatch):
    from remarkable_mcp.workflows.live import tools as live_tools

    _patch(monkeypatch, FakeWatcher())
    out = json.loads(asyncio.run(live_tools.remarkable_live_watch(timeout=5)))
    assert out["status"] == "no_change" and out["cursor"] == 0


def test_live_watch_tool_batches_and_filters(monkeypatch):
    from remarkable_mcp.workflows.live import tools as live_tools

    w = FakeWatcher(
        [
            live.Change("x", "Other", "/Other", "ink", ["p9"]),
            live.Change("a", "Sketch", "/Sketch", "ink", ["p1"]),
            live.Change("a", "Sketch", "/Sketch", "ink", ["p2"]),
        ]
    )
    _patch(monkeypatch, w, arrivals=[(0.1, 2), (0.2, 3)])
    out = json.loads(
        asyncio.run(
            live_tools.remarkable_live_watch("sketch", timeout=5, settle=0.5, analyse="none")
        )
    )
    assert out["status"] == "changed" and out["cursor"] == 3
    [change] = out["changes"]
    assert change["document"] == "Sketch" and change["changed_page_ids"] == ["p1", "p2"]
    # Shared events were not mutated by the merge.
    assert w.history[1][1].pages == ["p1"]


def test_live_watch_resumes_from_cursor(monkeypatch):
    from remarkable_mcp.workflows.live import tools as live_tools

    w = FakeWatcher([live.Change("a", "Sketch", "/Sketch", "ink", ["p1"])])
    w.seq = 1  # happened while the agent was busy with the previous answer
    _patch(monkeypatch, w)
    out = json.loads(
        asyncio.run(
            live_tools.remarkable_live_watch(
                "Sketch", timeout=5, settle=0.2, analyse="none", since=0
            )
        )
    )
    assert out["status"] == "changed" and out["changes"][0]["changed_page_ids"] == ["p1"]


def test_live_watch_settle_is_bounded_by_timeout(monkeypatch):
    import time as _time

    from remarkable_mcp.workflows.live import tools as live_tools

    changes = [live.Change("a", "S", "/S", "ink", [f"p{i}"]) for i in range(200)]
    w = FakeWatcher(changes)
    _patch(monkeypatch, w, arrivals=[(0.2, i) for i in range(1, 200)])  # never pauses
    t0 = _time.time()
    out = json.loads(
        asyncio.run(live_tools.remarkable_live_watch("S", timeout=5, settle=1.0, analyse="none"))
    )
    assert out["status"] == "changed"
    assert _time.time() - t0 < 5 + 1.0 + 1.5


def test_live_watch_cursor_from_before_a_restart(monkeypatch):
    from remarkable_mcp.workflows.live import tools as live_tools

    w = FakeWatcher([live.Change("a", "Sketch", "/Sketch", "ink", ["p1"])])
    w.seq = 1  # restarted server: seq starts again from 0 and is now at 1
    _patch(monkeypatch, w)
    out = json.loads(
        asyncio.run(
            live_tools.remarkable_live_watch(
                "Sketch", timeout=5, settle=0.2, analyse="none", since=17
            )
        )
    )
    assert out["status"] == "changed" and out["gap"] is True
