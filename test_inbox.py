"""Tests for the Agent Inbox: segmentation, identity across edits, tool loop."""

import asyncio

import pymupdf

from remarkable_mcp.workflows.inbox import (
    LINE_PITCH,
    TEMPLATE_TOP,
    match_known,
    render_inbox_template,
    segment_entries,
)
from remarkable_mcp.workflows.ink import load_document_ink_from_zip
from test_workflows import (  # noqa: F401  (fixtures)
    FINELINER,
    _doc_zip,
    _fake_path,
    _handwriting,
    _hline,
    _json_of,
    cloud,
)

PDF = render_inbox_template(pages=2)


def _line(n, words=3, x=40.0):
    """Handwriting sitting on ruled line n (1-based)."""
    y = TEMPLATE_TOP + n * LINE_PITCH - 12
    return _handwriting(x, y, words=words, h=8)


def _page(strokes):
    ink = load_document_ink_from_zip(_doc_zip(PDF, {0: [(s, FINELINER, 446.0) for s in strokes]}))
    return ink.pages[0]


def test_template_is_ruled_and_tablet_shaped():
    with pymupdf.open(stream=PDF, filetype="pdf") as doc:
        assert len(doc) == 2
        assert doc[0].rect.height / doc[0].rect.width > 1.3
        assert "Agent Inbox" in doc[0].get_text()


def test_blank_line_separates_entries_consecutive_lines_join():
    page = _page(_line(1) + _line(2, words=2) + _line(4) + _line(7, words=1))
    entries = segment_entries(page)
    assert [len(e.strokes) for e in entries] == [5, 3, 1]


def test_strike_through_cancels_but_underline_does_not():
    first = _line(1, words=4)
    second = _line(3, words=4)
    y_mid_first = TEMPLATE_TOP + LINE_PITCH - 12 + 4
    y_under_second = TEMPLATE_TOP + 3 * LINE_PITCH - 12 + 10
    strike = _hline(36, 160, y_mid_first)
    underline = _hline(36, 160, y_under_second)
    entries = segment_entries(_page(first + second + [strike, underline]))
    assert [e.cancelled for e in entries] == [True, False]


def test_entry_identity_survives_additions():
    before = segment_entries(_page(_line(1)))[0]
    after = segment_entries(_page(_line(1) + _line(2, words=1)))[0]
    stored = {"id": before.id, "fingerprints": sorted(before.fingerprints)}
    assert match_known(after, [stored]) is stored
    other = segment_entries(_page(_line(5)))[0]
    assert match_known(other, [stored]) is None


def test_inbox_tool_loop(cloud):  # noqa: F811
    from remarkable_mcp.workflows import inbox_tools as t

    setup = _json_of(asyncio.run(t.remarkable_inbox_setup(pages=2)))
    assert setup["uploaded"] is True
    doc = next(d for d in cloud.docs.values() if d.VissibleName == "Agent Inbox")
    assert _json_of(asyncio.run(t.remarkable_inbox()))["entries"] == []

    ink = [(s, FINELINER, 446.0) for s in _line(1) + _line(4)]
    cloud.annotate(doc.id, {0: ink})
    got = _json_of(asyncio.run(t.remarkable_inbox()))
    assert [e["status"] for e in got["entries"]] == ["pending", "pending"]
    first, second = (e["id"] for e in got["entries"])

    done = _json_of(asyncio.run(t.remarkable_inbox_done([first], replies={first: "Done: **ok**"})))
    assert done["done"] == [first]
    assert done["replies_document"].startswith("/Agent/Replies/Replies")
    assert any(d.VissibleName.startswith("Replies") for d in cloud.docs.values())

    pending = _json_of(asyncio.run(t.remarkable_inbox()))["entries"]
    assert [e["id"] for e in pending] == [second]

    # The user adds a line to the finished request: it needs action again.
    cloud.annotate(doc.id, {0: ink + [(s, FINELINER, 446.0) for s in _line(2, words=1)]})
    reopened = _json_of(asyncio.run(t.remarkable_inbox()))["entries"]
    assert {e["id"] for e in reopened} == {first, second}
    assert all(e["status"] == "pending" for e in reopened)

    everything = _json_of(asyncio.run(t.remarkable_inbox(pending_only=False)))["entries"]
    assert len(everything) == 2


def test_inbox_requires_setup(cloud):  # noqa: F811
    from remarkable_mcp.workflows import inbox_tools as t

    err = _json_of(asyncio.run(t.remarkable_inbox(name="nope")))
    assert err["_error"]["type"] == "inbox_not_set_up"
