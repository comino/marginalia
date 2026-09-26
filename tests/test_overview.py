"""Tests for remarkable_whats_new: one overview across workflows."""

import asyncio

from test_forms import _tick
from test_workflows import FINELINER, _fake_path, _hline, _json_of, cloud  # noqa: F401


def test_whats_new_across_workflows(cloud):  # noqa: F811
    from remarkable_mcp.workflows import overview as overview_tools
    from remarkable_mcp.workflows import tools
    from remarkable_mcp.workflows.forms import tools as form_tools
    from remarkable_mcp.workflows.inbox import tools as inbox_tools

    empty = _json_of(asyncio.run(overview_tools.remarkable_whats_new()))
    assert empty["tracked"] == 0

    asyncio.run(tools.remarkable_review_send(markdown="# Draft\n\nSome text to review here.\n"))
    ask = _json_of(asyncio.run(form_tools.remarkable_ask("Ship it?")))
    asyncio.run(inbox_tools.remarkable_inbox_setup(pages=1))

    quiet = _json_of(asyncio.run(overview_tools.remarkable_whats_new()))
    assert quiet["attention"] == [] and quiet["waiting"] == 3

    # Opening documents on the tablet changes metadata only: still quiet.
    for d in list(cloud.docs.values()):
        cloud.touch(d.id)
    assert _json_of(asyncio.run(overview_tools.remarkable_whats_new()))["attention"] == []

    review_doc = next(d for d in cloud.docs.values() if d.VissibleName.endswith("· v1"))
    cloud.annotate(review_doc.id, {0: [(_hline(60, 200, 120), FINELINER, 446.0)]})
    record = form_tools._forms().get(ask["form"])
    yes = next(a for a in record["manifest"]["areas"] if a["option"] == "Yes")
    cloud.annotate(record["doc_id"], {0: [(_tick(tuple(yes["rect"])), FINELINER, 446.0)]})

    news = _json_of(asyncio.run(overview_tools.remarkable_whats_new()))
    kinds = sorted(a["kind"] for a in news["attention"])
    assert kinds == ["form", "review"]
    nexts = {a["kind"]: a["next"] for a in news["attention"]}
    assert nexts["form"] == f"remarkable_form_read('{ask['form']}')"
    assert nexts["review"].startswith("remarkable_review_collect(")


def test_workflow_prompts_registered():
    from remarkable_mcp.server import mcp

    names = {p.name for p in asyncio.run(mcp.list_prompts())}
    assert {
        "tablet_check_in",
        "review_draft",
        "ask_on_tablet",
        "triage_on_paper",
        "meeting_pack",
        "research_on_paper",
        "daily_ink_digest",
    } <= names
