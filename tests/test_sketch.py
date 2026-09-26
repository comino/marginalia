"""Tests for sketch -> diagram recognition."""

import math

from remarkable_mcp.workflows.ink.page import Stroke
from remarkable_mcp.workflows.structure.sketch import (
    classify_outline,
    rdp,
    recognise,
    summary,
    to_mermaid,
    to_svg,
)
from test_workflows import _handwriting

_n = [0]


def S(points):
    _n[0] += 1
    return Stroke(index=_n[0], points=list(points), tool="fineliner", color="black", width=1)


def jitter(i):
    return 0.6 * math.sin(i * 1.7)


def rect_path(x0, y0, x1, y1, n=15):
    pts = []
    corners = [(x0, y0), (x1, y0), (x1, y1), (x0, y1), (x0, y0 + 1)]
    for (ax, ay), (bx, by) in zip(corners, corners[1:]):
        for t in range(n):
            pts.append(
                (
                    ax + (bx - ax) * t / n + jitter(len(pts)),
                    ay + (by - ay) * t / n + jitter(len(pts) + 3),
                )
            )
    return pts


def ellipse_path(cx, cy, rx, ry, n=60):
    return [
        (
            cx + rx * math.cos(2 * math.pi * i / n) + jitter(i),
            cy + ry * math.sin(2 * math.pi * i / n),
        )
        for i in range(n + 2)
    ]


def diamond_path(cx, cy, rx, ry, n=12):
    pts = []
    corners = [(cx, cy - ry), (cx + rx, cy), (cx, cy + ry), (cx - rx, cy), (cx + 1, cy - ry + 1)]
    for (ax, ay), (bx, by) in zip(corners, corners[1:]):
        for t in range(n):
            pts.append((ax + (bx - ax) * t / n, ay + (by - ay) * t / n))
    return pts


def line_path(a, b, n=25):
    return [
        (a[0] + (b[0] - a[0]) * t / (n - 1) + jitter(t) * 0.3, a[1] + (b[1] - a[1]) * t / (n - 1))
        for t in range(n)
    ]


def arrow_in_stroke(a, b):
    """Line a->b, then the pen draws the head: back-left, return to tip, back-right."""
    body = line_path(a, b)
    ang = math.atan2(b[1] - a[1], b[0] - a[0])
    left = (b[0] - 9 * math.cos(ang - 0.5), b[1] - 9 * math.sin(ang - 0.5))
    right = (b[0] - 9 * math.cos(ang + 0.5), b[1] - 9 * math.sin(ang + 0.5))
    return body + line_path(b, left, 6)[1:] + line_path(left, b, 6)[1:] + line_path(b, right, 6)[1:]


def v_head(tip, direction_deg):
    ang = math.radians(direction_deg)
    left = (tip[0] - 9 * math.cos(ang - 0.5), tip[1] - 9 * math.sin(ang - 0.5))
    right = (tip[0] - 9 * math.cos(ang + 0.5), tip[1] - 9 * math.sin(ang + 0.5))
    return line_path(left, tip, 6) + line_path(tip, right, 6)[1:]


def test_rdp_keeps_corners():
    pts = [(x, 0) for x in range(10)] + [(9, y) for y in range(1, 10)]
    assert rdp(pts, 0.5) == [(0, 0), (9, 0), (9, 9)]


def test_outline_classification():
    assert classify_outline(rect_path(0, 0, 100, 50), []).kind == "rect"
    assert classify_outline(ellipse_path(50, 50, 50, 25), []).kind == "ellipse"
    assert classify_outline(diamond_path(50, 50, 40, 30), []).kind == "diamond"
    assert classify_outline(line_path((0, 0), (100, 5)), []).kind == "line"
    arrow = classify_outline(arrow_in_stroke((0, 0), (100, 0)), [])
    assert arrow.kind == "arrow" and arrow.head_at_end
    assert math.dist(arrow.points[-1], (100, 0)) < 3


def build():
    strokes = [
        S(rect_path(50, 50, 150, 100)),
        S(ellipse_path(310, 75, 50, 25)),
        S(diamond_path(100, 260, 45, 35)),
        S(arrow_in_stroke((152, 75), (258, 75))),
        S(line_path((100, 102), (100, 222))),
        S(v_head((100, 224), 90)),
    ]
    labels = {
        "start": _handwriting(70, 70, words=2, word_w=18, h=6),
        "store": _handwriting(290, 72, words=1, word_w=18, h=6),
        "check": _handwriting(85, 257, words=1, word_w=18, h=6),
        "edge": _handwriting(185, 58, words=1, word_w=18, h=6),
    }
    for pts_list in labels.values():
        strokes += [S(p) for p in pts_list]
    return strokes


def test_full_diagram():
    d = recognise(build())
    kinds = sorted(n.shape.kind for n in d.nodes)
    assert kinds == ["diamond", "ellipse", "rect"]
    by_kind = {n.shape.kind: n for n in d.nodes}
    assert all(n.label is not None for n in d.nodes)
    edges = {(e.source, e.target): e for e in d.edges}
    rect_id, ell_id, dia_id = by_kind["rect"].id, by_kind["ellipse"].id, by_kind["diamond"].id
    assert (rect_id, ell_id) in edges and edges[(rect_id, ell_id)].directed
    assert edges[(rect_id, ell_id)].label is not None
    assert (rect_id, dia_id) in edges and edges[(rect_id, dia_id)].directed
    assert d.free_text == []


def test_outputs_render():
    d = recognise(build())
    for n in d.nodes:
        n.label.text = f"label {n.id}"
    mermaid = to_mermaid(d)
    assert mermaid.startswith("flowchart TD")
    assert "-->" in mermaid and '{"label' in mermaid and '(["label' in mermaid
    svg = to_svg(d)
    assert svg.startswith("<svg") and "<ellipse" in svg and 'fill="black"' in svg
    s = summary(d)
    assert len(s["nodes"]) == 3 and len(s["edges"]) == 2


def test_rectangle_drawn_in_two_strokes():
    path = rect_path(0, 0, 120, 60)
    half = len(path) // 2
    d = recognise([S(path[: half + 1]), S(path[half:])])
    assert [n.shape.kind for n in d.nodes] == ["rect"]


def test_empty_and_writing_only():
    assert recognise([]).nodes == []
    d = recognise([S(p) for p in _handwriting(10, 10, words=3)])
    assert d.nodes == [] and d.edges == [] and len(d.free_text) == 1


def test_sketch_and_regions_tools(cloud, monkeypatch):  # noqa: F811
    import asyncio

    import pymupdf

    from remarkable_mcp.workflows.structure import sketch_tools
    from test_workflows import FINELINER, _json_of

    doc = pymupdf.open()
    doc.new_page(width=446, height=595)
    pdf = doc.tobytes()
    diagram = [s.points for s in build()]
    notes = _handwriting(60, 480, words=3)
    uploaded = cloud.upload_document(pdf, "Whiteboard", "pdf")
    cloud.annotate(uploaded.id, {0: [(p, FINELINER, 446.0) for p in diagram + notes]})
    monkeypatch.setattr(
        "remarkable_mcp.core.tools._find_target_document",
        lambda items, by_id, name: next((d for d in items if d.VissibleName == name), None),
    )

    regions = _json_of(asyncio.run(sketch_tools.remarkable_regions("Whiteboard")))
    kinds = sorted(r["kind"] for r in regions["regions"])
    assert kinds == ["drawing", "writing"]
    drawing = next(r for r in regions["regions"] if r["kind"] == "drawing")

    got = asyncio.run(
        sketch_tools.remarkable_sketch("Whiteboard", region=drawing["rect"], include_images=True)
    )
    data = _json_of(got)
    assert sorted(n["shape"] for n in data["nodes"]) == ["diamond", "ellipse", "rect"]
    assert len(data["edges"]) == 2
    assert data["mermaid"].startswith("flowchart TD")
    assert any(getattr(b, "type", "") == "image" for b in got)


from test_workflows import _fake_path, cloud  # noqa: E402, F401


def test_two_boxes_sharing_a_corner_stay_two_nodes():
    a = rect_path(100, 50, 200, 110)
    b = [(x, y) for x, y in rect_path(100, 50, 200, 110)]
    b = [(100 + (100 - x), y) for x, y in b]  # mirrored box to the left, same corner
    d = recognise([S(a), S(b)])
    assert sorted(n.shape.kind for n in d.nodes) == ["rect", "rect"]


def test_arrow_to_diamond_side_attaches():
    dia = S(diamond_path(300, 100, 40, 30))
    # Tip on the middle of the upper-left side of the diamond.
    tip = (280, 85)
    arrow = S(arrow_in_stroke((150, 85), tip))
    box = S(rect_path(80, 60, 148, 110))
    d = recognise([box, dia, arrow])
    [edge] = d.edges
    kinds = {n.id: n.shape.kind for n in d.nodes}
    assert kinds[edge.target] == "diamond" and kinds[edge.source] == "rect"


def test_dense_page_is_fast():
    import time

    strokes = []
    for row in range(40):
        for col in range(10):
            strokes += [
                S(p) for p in _handwriting(20 + col * 40, 20 + row * 14, words=1, word_w=14, h=5)
            ]
    strokes += [S(line_path((10, 10 + 30 * i), (400, 12 + 30 * i))) for i in range(20)]
    t0 = time.time()
    recognise(strokes)
    assert time.time() - t0 < 3.0
