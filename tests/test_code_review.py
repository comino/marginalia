"""Tests for code review on paper: diff layout -> path:line comments."""

import asyncio

import pymupdf
import pytest

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
@@ -10,5 +10,6 @@ def reconnect(board):
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
    assert len(lines) == 7


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
    comments, _ = collect_comments(_pages(r, [strike, *note]), r.rows)
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
    [c], _ = collect_comments(_pages(r, [strike]), r.rows)
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


def _git(repo, *args):
    import subprocess

    return subprocess.run(
        ["git", "-C", str(repo), *args], capture_output=True, text=True, check=True
    ).stdout


def test_real_git_diff_edge_cases(tmp_path):
    """Content lines looking like headers, deleted files, odd separators, quoted paths."""
    repo = tmp_path / "r"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "t@t")
    _git(repo, "config", "user.name", "t")
    (repo / "q.sql").write_text("SELECT 1;\n-- x\nSELECT 2;\n")
    (repo / "c.c").write_text("int i;\n")
    (repo / "gone.txt").write_text("bye\n")
    (repo / "ff.py").write_text("a = 1\n\x0cb = 2\n")
    (repo / "ümlaut.txt").write_text("one\n")
    (repo / "crlf.txt").write_bytes(b"one\r\ntwo\r\n")
    (repo / "tabs.go").write_text("func f() {\n\treturn\n}")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "base")
    (repo / "q.sql").write_text("SELECT 1;\nSELECT 2;\n")  # removes "-- x"
    (repo / "c.c").write_text("int i;\n++ i;\nint j;\n")  # adds "++ i;"
    (repo / "gone.txt").unlink()
    (repo / "ff.py").write_text("a = 1\n\x0cb = 2\nc = 3\n")
    (repo / "ümlaut.txt").write_text("one\ntwo\n")
    (repo / "crlf.txt").write_bytes(b"one\r\nTWO\r\n")
    (repo / "tabs.go").write_text("func f() {\n\t\treturn nil\n}")
    diff = _git(repo, "diff")
    files = {f.path: f for f in parse_diff(diff)}
    assert set(files) == {"q.sql", "c.c", "gone.txt", "ff.py", "ümlaut.txt", "crlf.txt", "tabs.go"}

    sql = files["q.sql"].hunks[0][1]
    [removed] = [dl for dl in sql if dl.kind == "-"]
    assert removed.text == "-- x" and removed.old == 2
    assert [dl.new for dl in sql if dl.kind == " "][-1] == 2

    added = [dl for dl in files["c.c"].hunks[0][1] if dl.kind == "+"]
    assert [(dl.text, dl.new) for dl in added] == [("++ i;", 2), ("int j;", 3)]

    gone = files["gone.txt"].hunks[0][1]
    assert [dl.kind for dl in gone] == ["-"] and gone[0].old == 1

    ff_added = [dl for dl in files["ff.py"].hunks[0][1] if dl.kind == "+"]
    assert [(dl.text, dl.new) for dl in ff_added] == [("c = 3", 3)]

    crlf = [dl for dl in files["crlf.txt"].hunks[0][1] if dl.kind == "+"]
    assert crlf[0].text == "TWO" and crlf[0].new == 2

    render = render_diff("edge", list(files.values()))
    assert render.rows and all(r["line"] is not None for r in render.rows)
    for r in render.rows:
        assert (r["side"] == "LEFT") == (r["kind"] == "-")


def test_verdict_ticks_and_summaries_are_not_line_comments():
    r = render_diff("PR 7", parse_diff(DIFF))
    areas = [
        {"field": a.field_id, "option": a.option, "page": a.page, "rect": list(a.rect)}
        for a in r.verdict_areas
    ]
    changes = next(a for a in r.verdict_areas if a.option == "Request changes")
    last_row_bottom = max(x["y1"] for x in r.rows)
    summary = _handwriting(40, last_row_bottom + 60, words=3)  # written under the diff
    pages = _pages(r, [_tick(changes.rect), *summary])
    comments, general = collect_comments(pages, r.rows, exclude=areas)
    assert comments == []
    assert len(general) == 1 and general[0].kind == "note"
    assert read_verdict(areas, pages) == "Request changes"


@pytest.mark.parametrize(
    "bad",
    [
        {"pr": "--help"},
        {"pr": "7; rm -rf /"},
        {"pr": "7", "repo": "--jq=x"},
        {"repo_path": "/tmp", "base": "--output=/tmp/pwned"},
        {"repo_path": "/tmp", "base": "-p"},
    ],
)
def test_diff_arguments_cannot_become_options(bad):
    from remarkable_mcp.workflows.code_review_tools import _get_diff

    args = {
        "pr": None,
        "repo": None,
        "repo_path": None,
        "base": "origin/main",
        "paths": None,
        **bad,
    }
    with pytest.raises(ValueError):
        _get_diff(args["pr"], args["repo"], args["repo_path"], args["base"], args["paths"])


@pytest.mark.parametrize(
    "ref", ["origin/feature-x", "HEAD~3", "v1.2.3", "HEAD^2", "main@{1}", "release+1"]
)
def test_valid_refs_are_accepted(ref, monkeypatch):
    import subprocess

    from remarkable_mcp.workflows import code_review_tools as t

    seen = {}

    def fake_run(cmd, **kw):
        seen["cmd"] = cmd
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    monkeypatch.setattr(t.subprocess, "run", fake_run)
    t._get_diff(None, None, "/tmp", ref, None)
    assert f"{ref}...HEAD" in seen["cmd"] and "--no-textconv" in seen["cmd"]


def test_newline_in_ref_is_rejected():
    from remarkable_mcp.workflows.code_review_tools import _get_diff

    with pytest.raises(ValueError):
        _get_diff(None, None, "/tmp", "main\n", None)
