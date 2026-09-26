"""Tests for tables, wireframes, math blocks and the ink digest."""

import asyncio

import pymupdf

from remarkable_mcp.workflows.table import find_table
from remarkable_mcp.workflows.wireframe import build_wireframe, to_html
from test_sketch import S, ellipse_path, line_path, rect_path
from test_workflows import (  # noqa: F401
    FINELINER,
    _fake_path,
    _handwriting,
    _json_of,
    cloud,
)


def grid(x0, y0, col_w, row_h, rows, cols, boxed=False):
    strokes = []
    for r in range(rows + 1):
        if boxed and r in (0, rows):
            continue
        y = y0 + r * row_h
        strokes.append(S(line_path((x0, y), (x0 + cols * col_w, y + 0.5))))
    for c in range(cols + 1):
        if boxed and c in (0, cols):
            continue
        x = x0 + c * col_w
        strokes.append(S(line_path((x, y0), (x + 0.5, y0 + rows * row_h))))
    if boxed:
        strokes.append(S(rect_path(x0, y0, x0 + cols * col_w, y0 + rows * row_h, n=30)))
    return strokes


def test_table_grid_and_cells():
    strokes = grid(40, 60, 90, 30, rows=3, cols=3)
    for r, c in [(0, 0), (0, 2), (1, 1), (2, 2)]:
        strokes += [
            S(p) for p in _handwriting(40 + c * 90 + 10, 60 + r * 30 + 10, words=1, word_w=30, h=6)
        ]
    t = find_table(strokes)
    assert t.shape == (3, 3)
    filled = {(r, c) for r in range(3) for c in range(3) if t.cells[r][c]}
    assert filled == {(0, 0), (0, 2), (1, 1), (2, 2)}
    t.text = [["Name", "", "Qty"], ["", "x", ""], ["", "", "2"]]
    assert t.to_markdown().splitlines()[0] == "| Name |  | Qty |"
    assert t.to_csv().splitlines()[2] == ",,2"


def test_table_with_box_outline():
    t = find_table(grid(40, 60, 80, 28, rows=2, cols=4, boxed=True))
    assert t is not None and t.shape == (2, 4)


def test_no_table_in_plain_writing():
    assert find_table([S(p) for p in _handwriting(20, 20, words=5)]) is None


def test_wireframe_roles_and_html():
    strokes = [
        S(rect_path(20, 20, 400, 300)),  # card holding everything
        S(rect_path(40, 40, 200, 140)),  # image with an X
        S(line_path((42, 42), (198, 138))),
        S(line_path((42, 138), (198, 42))),
        S(rect_path(220, 60, 380, 84)),  # input
        S(rect_path(220, 100, 300, 126)),  # button with a label
        S(ellipse_path(350, 250, 18, 18)),  # round button
    ]
    strokes += [S(p) for p in _handwriting(230, 108, words=1, word_w=20, h=5)]
    elements = build_wireframe(strokes)
    roles = sorted(e.role for e in elements)
    assert roles == ["button", "button", "container", "image", "input"]
    card = next(e for e in elements if e.role == "container")
    assert {e.parent for e in elements if e is not card} == {card.id}
    page = to_html(elements, "Login")
    assert "<button" in page and "<input" in page and "img" in page
    with pymupdf.open(stream=page.encode(), filetype="html"):
        pass  # parses


def test_math_blocks_without_backend(cloud, monkeypatch):  # noqa: F811
    from remarkable_mcp.workflows import structure_tools as st

    doc = pymupdf.open()
    doc.new_page(width=446, height=595)
    uploaded = cloud.upload_document(doc.tobytes(), "Math", "pdf")
    eq1 = _handwriting(40, 60, words=3)
    eq2 = _handwriting(40, 200, words=2)
    cloud.annotate(uploaded.id, {0: [(p, FINELINER, 446.0) for p in eq1 + eq2]})
    monkeypatch.setattr(
        "remarkable_mcp.tools._find_target_document",
        lambda items, by_id, name: next((d for d in items if d.VissibleName == name), None),
    )
    out = _json_of(asyncio.run(st.remarkable_math("Math")))
    assert len(out["blocks"]) == 2 and all(b["latex"] is None for b in out["blocks"])


def test_ink_digest_reports_only_new_pages(cloud):  # noqa: F811
    from remarkable_mcp.workflows import structure_tools as st

    doc = pymupdf.open()
    for _ in range(3):
        doc.new_page(width=446, height=595)
    nb = cloud.upload_document(doc.tobytes(), "Notebook", "pdf")
    cloud.annotate(nb.id, {1: [(p, FINELINER, 446.0) for p in _handwriting(40, 60, words=3)]})
    first = _json_of(asyncio.run(st.remarkable_ink_digest()))
    [entry] = first["documents"]
    assert entry["document"] == "Notebook" and [p["page"] for p in entry["pages"]] == [2]
    assert _json_of(asyncio.run(st.remarkable_ink_digest()))["documents"] == []
    cloud.ink[nb.id][2] = [(p, FINELINER, 446.0) for p in _handwriting(40, 60, words=2)]
    cloud.annotate(nb.id, cloud.ink[nb.id])
    again = _json_of(asyncio.run(st.remarkable_ink_digest()))
    pages = [p["page"] for p in again["documents"][0]["pages"]]
    assert 3 in pages


def test_ink_digest_never_drops_unreported_pages(cloud):  # noqa: F811
    from remarkable_mcp.workflows import structure_tools as st

    doc = pymupdf.open()
    for _ in range(9):
        doc.new_page(width=446, height=595)
    nb = cloud.upload_document(doc.tobytes(), "Long notebook", "pdf")
    cloud.annotate(
        nb.id, {i: [(p, FINELINER, 446.0) for p in _handwriting(40, 60, words=2)] for i in range(9)}
    )
    peek = _json_of(asyncio.run(st.remarkable_ink_digest(mark_seen=False)))
    assert [p["page"] for p in peek["documents"][0]["pages"]] == [1, 2, 3, 4, 5, 6]
    first = _json_of(asyncio.run(st.remarkable_ink_digest()))
    assert first["documents"][0]["more_pages"] == 3 and "call again" in first["_hint"]
    rest = _json_of(asyncio.run(st.remarkable_ink_digest()))
    assert [p["page"] for p in rest["documents"][0]["pages"]] == [7, 8, 9]
    assert _json_of(asyncio.run(st.remarkable_ink_digest()))["documents"] == []


def test_table_with_rules_drawn_per_cell():
    strokes = []
    x0, y0, cw, rh, rows, cols = 40, 60, 70, 30, 3, 3
    for r in range(rows + 1):  # horizontal rules drawn one cell at a time
        for c in range(cols):
            strokes.append(
                S(line_path((x0 + c * cw + 1, y0 + r * rh), (x0 + (c + 1) * cw - 1, y0 + r * rh)))
            )
    for c in range(cols + 1):  # vertical rules drawn one cell at a time
        for r in range(rows):
            strokes.append(
                S(line_path((x0 + c * cw, y0 + r * rh + 1), (x0 + c * cw, y0 + (r + 1) * rh - 1)))
            )
    t = find_table(strokes)
    assert t is not None and t.shape == (3, 3)
