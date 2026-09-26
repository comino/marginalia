"""Tests for code review on paper: diff layout -> path:line comments."""

import asyncio

import pymupdf

from remarkable_mcp.workflows.code_review import (
    collect_comments,
    comment_body,
    github_event,
    parse_diff,
    read_verdict,
    render_diff,
)
from remarkable_mcp.workflows.ink import load_document_ink_from_zip
from test_forms import _tick
from test_workflows import (  # noqa: F401
    FINELINER,
    _doc_zip,
    _fake_path,
    _handwriting,
    _hline,
    _json_of,
    cloud,
)

DIFF = """diff --git a/app/sync.py b/app/sync.py
index 1111111..2222222 100644
--- a/app/sync.py
+++ b/app/sync.py
@@ -10,6 +10,7 @@ def reconnect(board):
     state = load(board)
-    retries = 3
+    retries = compute_retries(board, backoff=2)
+    log.info("reconnecting board %s", board.id)
     for attempt in range(retries):
         if connect(board):
             return True
diff --git a/README.md b/README.md
--- a/README.md
+++ b/README.md
@@ -1,2 +1,2 @@
-Old title
+New title
 Body
"""


def test_parse_diff_tracks_line_numbers():
    files = parse_diff(DIFF)
    assert [f.path for f in files] == ["app/sync.py", "README.md"]
    lines = files[0].hunks[0][1]
    removed = next(dl for dl in lines if dl.kind == "-")
    added = [dl for dl in lines if dl.kind == "+"]
    assert removed.old == 11 and removed.new is None
    assert [a.new for a in added] == [11, 12]
    assert lines[-1].old == 14 and lines[-1].new == 15


def _row(render, path, line, side="RIGHT"):
    return next(
        r for r in render.rows if r["path"] == path and r["line"] == line and r["side"] == side
    )


def _pages(render, strokes):
    ink = load_document_ink_from_zip(
        _doc_zip(render.pdf, {0: [(p, FINELINER, 446.0) for p in strokes]})
    )
    return {p.pdf_page + 1: p for p in ink.pages}


def _code_words(render, row, text):
    with pymupdf.open(stream=render.pdf, filetype="pdf") as doc:
        words = doc[row["page"] - 1].get_text("words")
    return [
        w for w in words if text in w[4] and row["y0"] - 1 <= (w[1] + w[3]) / 2 <= row["y1"] + 1
    ]


def test_strike_and_margin_note_map_to_lines():
    r = render_diff("PR 7", parse_diff(DIFF))
    row = _row(r, "app/sync.py", 12)
    [w] = _code_words(r, row, "reconnecting")
    strike = _hline(w[0], w[2], (w[1] + w[3]) / 2 + 0.3)
    target_row = _row(r, "app/sync.py", 11)
    note = _handwriting(446 - 95, target_row["y0"], words=2, word_w=16, h=5)
    comments = collect_comments(_pages(r, [strike, *note]), r.rows)
    by_line = {(c.path, c.line): c for c in comments}
    assert by_line[("app/sync.py", 12)].kind == "strikethrough"
    assert by_line[("app/sync.py", 11)].kind == "note"
    body = comment_body(by_line[("app/sync.py", 12)], None)
    assert body.startswith("Remove `")


def test_removed_line_comments_use_left_side():
    r = render_diff("PR 7", parse_diff(DIFF))
    row = _row(r, "app/sync.py", 11, side="LEFT")
    [w] = [x for x in _code_words(r, row, "3") if x[4] == "3"]
    strike = _hline(w[0] - 20, w[2], (w[1] + w[3]) / 2 + 0.3)
    [c] = collect_comments(_pages(r, [strike]), r.rows)
    assert (c.side, c.line) == ("LEFT", 11)


def test_verdict_boxes():
    r = render_diff("PR 7", parse_diff(DIFF))
    areas = [
        {"field": a.field_id, "option": a.option, "page": a.page, "rect": list(a.rect)}
        for a in r.verdict_areas
    ]
    approve = next(a for a in r.verdict_areas if a.option == "Approve")
    pages = _pages(r, [_tick(approve.rect)])
    assert read_verdict(areas, pages) == "Approve"
    assert github_event("Approve") == "APPROVE" and github_event(None) == "COMMENT"


def test_code_review_tool_round_trip(cloud, monkeypatch):  # noqa: F811
    from remarkable_mcp.workflows import code_review_tools as t

    monkeypatch.setattr(t, "_get_diff", lambda *a: DIFF)
    sent = _json_of(asyncio.run(t.remarkable_code_review_send(pr="7", repo="me/app")))
    assert sent["files"] == 2 and sent["changed_lines"] == 5
    record = t._store().get(sent["review"])
    doc = next(d for d in cloud.docs.values() if d.VissibleName.startswith("Code review"))
    render_rows = record["rows"]
    row = next(
        x
        for x in render_rows
        if x["path"] == "README.md" and x["line"] == 1 and x["side"] == "RIGHT"
    )
    words = [
        w
        for w in pymupdf.open(stream=cloud.zips[doc.id], filetype="pdf")[row["page"] - 1].get_text(
            "words"
        )
        if w[4] == "title" and row["y0"] - 1 <= (w[1] + w[3]) / 2 <= row["y1"] + 1
    ]
    w = words[0]
    cloud.annotate(doc.id, {row["page"] - 1: [(_hline(w[0], w[2], w[3] + 0.8), FINELINER, 446.0)]})
    got = _json_of(asyncio.run(t.remarkable_code_review_collect(sent["review"])))
    [c] = got["comments"]
    assert (c["path"], c["line"], c["side"], c["mark"]) == ("README.md", 1, "RIGHT", "underline")
    assert got["github"]["event"] == "COMMENT"
    assert got["github"]["comments"][0]["path"] == "README.md"
    assert "gh api repos/me/app/pulls/7/reviews" in got["_hint"]
    assert _json_of(asyncio.run(t.remarkable_code_review_collect(sent["review"])))["comments"] == []
