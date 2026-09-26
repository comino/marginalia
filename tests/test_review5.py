"""Regression tests for the fifth review round (whole-system findings)."""

import asyncio

import pytest

from test_workflows import (  # noqa: F401
    DRAFT,
    FINELINER,
    _fake_path,
    _json_of,
    _page_w,
    _phrase_rects,
    _strike,
    cloud,
)


def test_whats_new_covers_code_reviews(cloud, monkeypatch):  # noqa: F811
    from remarkable_mcp.workflows import overview as overview_tools
    from remarkable_mcp.workflows.review import code_tools as t
    from test_code_review import DIFF

    monkeypatch.setattr(t, "_get_diff", lambda *a: DIFF)
    sent = _json_of(asyncio.run(t.remarkable_code_review_send(pr="7", repo="me/app")))
    doc = next(d for d in cloud.docs.values() if d.VissibleName.startswith("Code review"))
    assert _json_of(asyncio.run(overview_tools.remarkable_whats_new()))["attention"] == []
    cloud.annotate(doc.id, {0: [([(60, 100), (200, 101)], FINELINER, 446.0)]})
    [item] = _json_of(asyncio.run(overview_tools.remarkable_whats_new()))["attention"]
    assert item["kind"] == "code_review"
    assert item["next"] == f"remarkable_code_review_collect('{sent['review']}')"


def test_review_peek_keeps_the_review_annotated(cloud):  # noqa: F811
    from remarkable_mcp.workflows import tools

    sent = _json_of(asyncio.run(tools.remarkable_review_send(markdown=DRAFT)))
    doc = next(d for d in cloud.docs.values() if d.VissibleName.endswith("· v1"))
    pdf = cloud.zips[doc.id]
    pno, rects = _phrase_rects(pdf, "permission to fetch data")
    cloud.annotate(doc.id, {pno: [(_strike(rects), FINELINER, _page_w(pdf))]})
    asyncio.run(tools.remarkable_review_collect(sent["review"], mark_seen=False))
    listed = _json_of(asyncio.run(tools.remarkable_review_list()))
    assert listed["reviews"][0]["status"] == "annotated"  # the peek consumed nothing


@pytest.mark.parametrize("bad", ["a b", "x/y", "../up", ""])
def test_malformed_ids_are_simply_not_found(cloud, bad):  # noqa: F811
    from remarkable_mcp.workflows.forms import tools as form_tools
    from remarkable_mcp.workflows.reading import tools as reading_tools
    from remarkable_mcp.workflows.review import code_tools as code_review_tools
    from remarkable_mcp.workflows.review import latex_tools

    for call, err in [
        (form_tools.remarkable_form_read(bad), "form_not_found"),
        (code_review_tools.remarkable_code_review_collect(bad), "review_not_found"),
        (latex_tools.remarkable_latex_review_collect(bad), "review_not_found"),
    ]:
        assert _json_of(asyncio.run(call))["_error"]["type"] == err
    if bad:
        assert (
            _json_of(asyncio.run(reading_tools.remarkable_reading_notes(bad)))["_error"]["type"]
            == "item_not_found"
        )


def test_null_reply_in_responses(cloud):  # noqa: F811
    from remarkable_mcp.workflows import tools

    first = _json_of(asyncio.run(tools.remarkable_review_send(markdown=DRAFT)))
    second = _json_of(
        asyncio.run(
            tools.remarkable_review_send(
                markdown=DRAFT + "\nMore.\n",
                review=first["review"],
                responses=[{"id": "m1", "status": None, "reply": None}],
            )
        )
    )
    assert second["version"] == 2


def test_read_only_mode_registers_no_writers(tmp_path, monkeypatch):
    from mcp.server.mcpserver import MCPServer

    from remarkable_mcp.trmnl import tools as trmnl_tools
    from remarkable_mcp.workflows.forms import tools as form_tools
    from remarkable_mcp.workflows.inbox import tools as inbox_tools
    from remarkable_mcp.workflows.review import code_tools as code_review_tools
    from remarkable_mcp.workflows.review import latex_tools

    server = MCPServer(name="ro")
    trmnl_tools.register(server, write_enabled=False)
    for m in (form_tools, inbox_tools, code_review_tools, latex_tools):
        m.register(server, False)
    names = {t.name for t in asyncio.run(server.list_tools())}
    assert {"trmnl_status", "trmnl_get", "trmnl_dashboard"} <= names
    writers = {
        "trmnl_set_slots",
        "trmnl_push",
        "trmnl_clear",
        "trmnl_image",
        "trmnl_send",
        "remarkable_form_send",
        "remarkable_ask",
        "remarkable_clarify",
        "remarkable_triage_send",
        "remarkable_inbox_setup",
        "remarkable_code_review_send",
        "remarkable_latex_review_send",
    }
    assert not names & writers


def test_idle_shared_watcher_stops(monkeypatch):
    from remarkable_mcp.workflows.live import watcher as live

    real_sleep = asyncio.sleep

    async def fast_sleep(s, *a, **k):
        await real_sleep(min(s, 0.01))

    monkeypatch.setattr(live.asyncio, "sleep", fast_sleep)
    monkeypatch.setattr(live, "IDLE_SHUTDOWN_SECONDS", 0.05)
    monkeypatch.setattr(live, "_shared", None)
    monkeypatch.setattr(live, "_task", None)
    monkeypatch.setattr(live, "_reaper", None)

    class Idle(live.Watcher):
        async def run(self):
            self.state = {}
            await real_sleep(3600)

    monkeypatch.setattr(live, "Watcher", lambda: Idle(take_snapshot=lambda: {}, connect=None))

    async def scenario():
        w = await live.shared_watcher()
        task = live._task
        await real_sleep(0.3)
        return w, task

    w, task = asyncio.run(scenario())
    assert task.cancelled() or task.done()
    assert w.mode == "stopped (idle)" and live._task is None
