"""Sketch -> diagram: recognise boxes, ellipses, diamonds and arrows in ink.

Geometric, dependency-free recognition tuned for whiteboard-style sketches:

1. Split strokes into *shape* candidates (large, or long and straight) and
   *writing* (small strokes, clustered into labels).
2. Simplify each shape stroke with Ramer-Douglas-Peucker and classify it:
   closed -> rect / diamond / triangle / ellipse; open -> line or arrow
   (an arrowhead drawn in the same stroke, or as a separate small V near an
   end). Rectangles drawn in several strokes are chained by their endpoints.
3. Labels inside a shape name the node; labels beside a connector name the
   edge. Connector ends snap to the nearest shape boundary.

The result is a small graph (nodes, edges, free text), rendered as Mermaid
and as a clean SVG.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence, Tuple

from remarkable_mcp.workflows.ink import Rect, Stroke
from remarkable_mcp.workflows.marks import _cluster, _union, rect_distance

Point = Tuple[float, float]


# --------------------------------------------------------------------------- geometry helpers


def rdp(points: Sequence[Point], epsilon: float) -> List[Point]:
    """Ramer-Douglas-Peucker polyline simplification (iterative)."""
    if len(points) < 3:
        return list(points)
    keep = [False] * len(points)
    keep[0] = keep[-1] = True
    stack = [(0, len(points) - 1)]
    while stack:
        a, b = stack.pop()
        ax, ay = points[a]
        bx, by = points[b]
        dx, dy = bx - ax, by - ay
        norm = math.hypot(dx, dy)
        best, idx = 0.0, -1
        for i in range(a + 1, b):
            px, py = points[i]
            if norm == 0:
                d = math.hypot(px - ax, py - ay)
            else:
                d = abs(dy * px - dx * py + bx * ay - by * ax) / norm
            if d > best:
                best, idx = d, i
        if best > epsilon and idx > 0:
            keep[idx] = True
            stack.append((a, idx))
            stack.append((idx, b))
    return [p for p, k in zip(points, keep) if k]


def _angle(a: Point, b: Point, c: Point) -> float:
    """Interior angle at b in degrees."""
    v1 = (a[0] - b[0], a[1] - b[1])
    v2 = (c[0] - b[0], c[1] - b[1])
    n1, n2 = math.hypot(*v1), math.hypot(*v2)
    if n1 == 0 or n2 == 0:
        return 180.0
    cos = max(-1.0, min(1.0, (v1[0] * v2[0] + v1[1] * v2[1]) / (n1 * n2)))
    return math.degrees(math.acos(cos))


def _path_length(points: Sequence[Point]) -> float:
    return sum(math.dist(a, b) for a, b in zip(points, points[1:]))


def _bbox(points: Sequence[Point]) -> Rect:
    xs = [p[0] for p in points]
    ys = [p[1] for p in points]
    return min(xs), min(ys), max(xs), max(ys)


def _ellipse_error(points: Sequence[Point], rect: Rect) -> float:
    """Mean relative deviation of points from the ellipse inscribed in ``rect``."""
    cx, cy = (rect[0] + rect[2]) / 2, (rect[1] + rect[3]) / 2
    rx, ry = max((rect[2] - rect[0]) / 2, 1e-6), max((rect[3] - rect[1]) / 2, 1e-6)
    errs = [abs(math.hypot((x - cx) / rx, (y - cy) / ry) - 1.0) for x, y in points]
    return sum(errs) / len(errs)


def _point_segment_distance(p: Point, a: Point, b: Point) -> float:
    dx, dy = b[0] - a[0], b[1] - a[1]
    if dx == dy == 0:
        return math.dist(p, a)
    t = max(0.0, min(1.0, ((p[0] - a[0]) * dx + (p[1] - a[1]) * dy) / (dx * dx + dy * dy)))
    return math.dist(p, (a[0] + t * dx, a[1] + t * dy))


# --------------------------------------------------------------------------- model


@dataclass
class Shape:
    kind: str  # rect | diamond | ellipse | triangle | line | arrow
    rect: Rect
    strokes: List[Stroke]
    points: List[Point] = field(default_factory=list)  # simplified outline / polyline
    head_at_end: bool = False  # arrows: arrowhead at points[-1]
    head_at_start: bool = False

    @property
    def is_node(self) -> bool:
        return self.kind in ("rect", "diamond", "ellipse", "triangle")


@dataclass
class Label:
    strokes: List[Stroke]
    rect: Rect
    text: Optional[str] = None


@dataclass
class Node:
    id: str
    shape: Shape
    label: Optional[Label] = None


@dataclass
class Edge:
    source: Optional[str]
    target: Optional[str]
    shape: Shape
    directed: bool
    label: Optional[Label] = None


@dataclass
class Diagram:
    nodes: List[Node]
    edges: List[Edge]
    free_text: List[Label]
    rect: Rect
    loose_lines: int = 0  # straight strokes attached to no shape (ignored)

    @property
    def connected(self) -> int:
        """Edges joining two different shapes."""
        return sum(1 for e in self.edges if e.source and e.target and e.source != e.target)

    @property
    def is_diagram(self) -> bool:
        return self.connected >= 1 or len(self.nodes) >= 3

    def labels(self) -> List[Label]:
        out = [n.label for n in self.nodes if n.label]
        out += [e.label for e in self.edges if e.label]
        return out + self.free_text


# --------------------------------------------------------------------------- recognition


def _is_closed(stroke: Stroke) -> bool:
    x0, y0, x1, y1 = stroke.bbox
    size = max(x1 - x0, y1 - y0) or 1e-6
    return math.dist(stroke.points[0], stroke.points[-1]) / size < 0.25


def _merge_open_strokes(strokes: List[Stroke], gap: float) -> List[List[Stroke]]:
    """Chain open strokes whose endpoints meet into candidate multi-stroke outlines.

    Strokes that already close on themselves stay alone (two boxes drawn from a
    shared corner are two shapes). Endpoints are bucketed on a grid of ``gap``
    so the join is near-linear even on dense pages.
    """
    parent = list(range(len(strokes)))

    def find(i: int) -> int:
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    grid: Dict[Tuple[int, int], List[Tuple[int, Point]]] = {}
    for i, st in enumerate(strokes):
        if _is_closed(st):
            continue
        for p in (st.points[0], st.points[-1]):
            cell = (int(p[0] // gap), int(p[1] // gap))
            for dx in (-1, 0, 1):
                for dy in (-1, 0, 1):
                    for j, q in grid.get((cell[0] + dx, cell[1] + dy), ()):
                        if j != i and math.dist(p, q) <= gap:
                            parent[find(i)] = find(j)
            grid.setdefault(cell, []).append((i, p))
    groups: Dict[int, List[Stroke]] = {}
    for i, st in enumerate(strokes):
        groups.setdefault(find(i), []).append(st)
    return list(groups.values())


def _chain_ends(strokes: Sequence[Stroke]) -> List[Point]:
    return [p for s in strokes for p in (s.points[0], s.points[-1])]


def _ordered_chain(strokes: Sequence[Stroke]) -> List[Point]:
    """Concatenate strokes into one path, flipping them so ends meet."""
    remaining = list(strokes)
    path = list(remaining.pop(0).points)
    while remaining:
        tail = path[-1]
        best, flip, idx = math.inf, False, 0
        for k, s in enumerate(remaining):
            for rev, end in ((False, s.points[0]), (True, s.points[-1])):
                d = math.dist(tail, end)
                if d < best:
                    best, flip, idx = d, rev, k
        s = remaining.pop(idx)
        path += list(reversed(s.points)) if flip else list(s.points)
    return path


def classify_outline(points: Sequence[Point], strokes: List[Stroke]) -> Optional[Shape]:
    """Classify a (possibly chained) path as a node shape or connector."""
    if len(points) < 2:
        return None
    rect = _bbox(points)
    w, h = rect[2] - rect[0], rect[3] - rect[1]
    size = max(w, h)
    if size < 8:
        return None
    length = _path_length(points)
    closure = math.dist(points[0], points[-1]) / size

    if closure < 0.25 and length > 2.2 * size and min(w, h) > 0.2 * size:
        simple = rdp(points, 0.07 * size)
        if len(simple) > 2 and math.dist(simple[0], simple[-1]) < 0.25 * size:
            simple = simple[:-1]
        corners = [
            simple[i]
            for i in range(len(simple))
            if _angle(simple[i - 1], simple[i], simple[(i + 1) % len(simple)]) < 145
        ]
        if len(corners) == 4:
            cx, cy = (rect[0] + rect[2]) / 2, (rect[1] + rect[3]) / 2
            mid_sides = sum(1 for x, y in corners if abs(x - cx) < 0.2 * w or abs(y - cy) < 0.2 * h)
            kind = "diamond" if mid_sides >= 3 else "rect"
            return Shape(kind, rect, strokes, corners)
        if len(corners) == 3:
            return Shape("triangle", rect, strokes, corners)
        if _ellipse_error(points, rect) < 0.16:
            return Shape("ellipse", rect, strokes, list(simple))
        if len(corners) in (5, 6) and _ellipse_error(points, rect) >= 0.16:
            return Shape("rect", rect, strokes, corners)
        return Shape("ellipse", rect, strokes, list(simple))

    simple = rdp(points, max(1.5, 0.05 * size))
    shape = Shape("line", rect, strokes, simple)
    # Arrowhead drawn in the same stroke: the path ends by folding back sharply.
    for at_end in (True, False):
        pts = simple if at_end else list(reversed(simple))
        if len(pts) >= 3:
            last = math.dist(pts[-1], pts[-2])
            body = _path_length(pts[:-1])
            turn = _angle(pts[-3], pts[-2], pts[-1])
            if last < 0.35 * body and turn < 60:
                shape.kind = "arrow"
                trimmed = pts[:-1]
                if len(trimmed) >= 3 and _angle(trimmed[-3], trimmed[-2], trimmed[-1]) < 60:
                    trimmed = trimmed[:-1]  # V drawn as two folds
                shape.points = trimmed if at_end else list(reversed(trimmed))
                if at_end:
                    shape.head_at_end = True
                else:
                    shape.head_at_start = True
                break
    return shape


def _v_tip(stroke: Stroke) -> Optional[Point]:
    """Tip of a small V-shaped stroke (an arrowhead candidate), else None."""
    sx0, sy0, sx1, sy1 = stroke.bbox
    size = max(sx1 - sx0, sy1 - sy0)
    if size < 3:
        return None
    simple = rdp(stroke.points, max(0.8, 0.12 * size))
    if len(simple) != 3 or _angle(*simple) > 110:
        return None
    return simple[1]


def _is_arrowhead(stroke: Stroke, line: Shape, tip: Optional[Point]) -> Optional[bool]:
    """A small V next to one end of ``line``: returns True (end) / False (start) / None."""
    sx0, sy0, sx1, sy1 = stroke.bbox
    size = max(sx1 - sx0, sy1 - sy0)
    if tip is None or size > 0.5 * _path_length(line.points):
        return None
    start, end = line.points[0], line.points[-1]
    reach = max(6.0, 0.8 * size)
    if math.dist(tip, end) <= reach:
        return True
    if math.dist(tip, start) <= reach:
        return False
    return None


def _node_boundary_distance(p: Point, shape: Shape) -> float:
    x0, y0, x1, y1 = shape.rect
    if shape.kind == "ellipse":
        cx, cy = (x0 + x1) / 2, (y0 + y1) / 2
        rx, ry = max((x1 - x0) / 2, 1e-6), max((y1 - y0) / 2, 1e-6)
        r = math.hypot((p[0] - cx) / rx, (p[1] - cy) / ry)
        return abs(r - 1.0) * min(rx, ry)
    if shape.kind in ("diamond", "triangle") and len(shape.points) >= 3:
        if shape.kind == "diamond":
            cx, cy = (x0 + x1) / 2, (y0 + y1) / 2
            poly = [(cx, y0), (x1, cy), (cx, y1), (x0, cy)]
        else:
            poly = list(shape.points)
        return min(_point_segment_distance(p, a, b) for a, b in zip(poly, poly[1:] + poly[:1]))
    inside = x0 <= p[0] <= x1 and y0 <= p[1] <= y1
    if inside:
        return min(p[0] - x0, x1 - p[0], p[1] - y0, y1 - p[1])
    return rect_distance((p[0], p[1], p[0], p[1]), shape.rect)


def recognise(
    strokes: Sequence[Stroke],
    shape_min: float = 28.0,
    region: Optional[Rect] = None,
) -> Diagram:
    """Build a diagram from the strokes (optionally only those inside ``region``)."""
    pool = [s for s in strokes if not s.is_highlighter and len(s.points) >= 2]
    if region is not None:
        pool = [s for s in pool if rect_distance(s.bbox, region) == 0]
    if not pool:
        return Diagram([], [], [], region or (0, 0, 0, 0))

    big, small = [], []
    for s in pool:
        x0, y0, x1, y1 = s.bbox
        size = max(x1 - x0, y1 - y0)
        straight = math.dist(s.points[0], s.points[-1]) / (s.length or 1e-6)
        if size >= shape_min or (size >= 0.55 * shape_min and straight > 0.9):
            big.append(s)
        else:
            small.append(s)

    shapes: List[Shape] = []
    for group in _merge_open_strokes(big, gap=6.0):
        path = _ordered_chain(group) if len(group) > 1 else list(group[0].points)
        shape = classify_outline(path, group)
        if shape is None:
            small.extend(group)
            continue
        if shape.kind in ("line", "arrow") and len(group) > 1:
            # Chaining only makes sense for closed outlines; classify parts alone.
            for s in group:
                part = classify_outline(list(s.points), [s])
                if part is not None:
                    shapes.append(part)
                else:
                    small.append(s)
            continue
        shapes.append(shape)

    # Separate arrowheads (small V strokes at a connector end).
    connectors = [s for s in shapes if not s.is_node]
    writing: List[Stroke] = []
    ends = [(p, c) for c in connectors for p in (c.points[0], c.points[-1])]
    for s in small:
        claimed = False
        size = max(s.bbox[2] - s.bbox[0], s.bbox[3] - s.bbox[1])
        reach = max(6.0, 0.8 * size)
        # Cheap geometric gate first; simplify the stroke only near a connector end.
        near = [c for p, c in ends if rect_distance(s.bbox, (p[0], p[1], p[0], p[1])) <= reach]
        tip = _v_tip(s) if near else None
        for c in {id(c): c for c in near}.values():
            side = _is_arrowhead(s, c, tip)
            if side is not None:
                c.kind = "arrow"
                c.strokes.append(s)
                if side:
                    c.head_at_end = True
                else:
                    c.head_at_start = True
                claimed = True
                break
        if not claimed:
            writing.append(s)

    labels: List[Label] = []
    if writing:
        for g in _cluster([s.bbox for s in writing], 7.0, 4.0):
            group = sorted((writing[i] for i in g), key=lambda s: s.index)
            labels.append(Label(group, _union([s.bbox for s in group])))

    nodes = [
        Node(f"n{i + 1}", s)
        for i, s in enumerate(
            sorted((s for s in shapes if s.is_node), key=lambda s: (s.rect[1], s.rect[0]))
        )
    ]
    edges: List[Edge] = []
    for c in (s for s in shapes if not s.is_node):
        ends = [c.points[0], c.points[-1]]
        attached: List[Optional[str]] = []
        for p in ends:
            best = min(nodes, key=lambda n: _node_boundary_distance(p, n.shape), default=None)
            if best is not None and _node_boundary_distance(p, best.shape) <= 14:
                attached.append(best.id)
            else:
                attached.append(None)
        src, dst = attached
        directed = c.kind == "arrow"
        if c.head_at_start and not c.head_at_end:
            src, dst = dst, src
        if src is not None and src == dst:
            continue  # loops back onto its own shape: a doubled outline, not an edge
        edges.append(Edge(src, dst, c, directed))

    free: List[Label] = []
    for lab in labels:
        cx, cy = (lab.rect[0] + lab.rect[2]) / 2, (lab.rect[1] + lab.rect[3]) / 2
        owner = next(
            (
                n
                for n in sorted(
                    nodes,
                    key=lambda n: (n.shape.rect[2] - n.shape.rect[0])
                    * (n.shape.rect[3] - n.shape.rect[1]),
                )
                if n.shape.rect[0] <= cx <= n.shape.rect[2]
                and n.shape.rect[1] <= cy <= n.shape.rect[3]
            ),
            None,
        )
        if owner is not None:
            owner.label = _merge_label(owner.label, lab)
            continue
        near = min(
            edges,
            key=lambda e: _min_segment_distance((cx, cy), e.shape.points),
            default=None,
        )
        if near is not None and _min_segment_distance((cx, cy), near.shape.points) <= 18:
            near.label = _merge_label(near.label, lab)
            continue
        free.append(lab)

    # Connectors touching no shape are underlines/dividers; one-ended plain
    # lines are usually decoration. Keep arrows and labelled lines.
    kept: List[Edge] = []
    loose = 0
    for e in edges:
        ends = (e.source is not None) + (e.target is not None)
        if ends == 2 or (ends == 1 and (e.directed or e.label is not None)):
            kept.append(e)
        else:
            loose += 1
            if e.label is not None:
                free.append(e.label)
    all_rects = (
        [n.shape.rect for n in nodes] + [e.shape.rect for e in kept] + [lab.rect for lab in labels]
    )
    d = Diagram(nodes, kept, free, _union(all_rects) if all_rects else (0, 0, 0, 0))
    d.loose_lines = loose
    return d


def _merge_label(existing: Optional[Label], new: Label) -> Label:
    if existing is None:
        return new
    strokes = sorted(existing.strokes + new.strokes, key=lambda s: s.index)
    return Label(strokes, _union([existing.rect, new.rect]))


def _min_segment_distance(p: Point, pts: Sequence[Point]) -> float:
    if len(pts) == 1:
        return math.dist(p, pts[0])
    return min(_point_segment_distance(p, a, b) for a, b in zip(pts, pts[1:]))


# --------------------------------------------------------------------------- output


def _mermaid_text(text: Optional[str], fallback: str) -> str:
    t = " ".join((text or "").split()) or fallback
    return t.replace('"', "'")


def to_mermaid(d: Diagram) -> str:
    shapes = {
        "rect": ('["', '"]'),
        "diamond": ('{"', '"}'),
        "ellipse": ('(["', '"])'),
        "triangle": ('[/"', '"\\]'),
    }
    lines = ["flowchart TD"]
    for n in d.nodes:
        o, c = shapes.get(n.shape.kind, ('["', '"]'))
        lines.append(f"    {n.id}{o}{_mermaid_text(n.label.text if n.label else None, n.id)}{c}")
    loose = 0
    for e in d.edges:
        src, dst = e.source, e.target
        if src is None:
            loose += 1
            src = f"x{loose}"
            lines.append(f"    {src}(( ))")
        if dst is None:
            loose += 1
            dst = f"x{loose}"
            lines.append(f"    {dst}(( ))")
        arrow = "-->" if e.directed else "---"
        if e.label is not None:
            lines.append(f'    {src} {arrow}|"{_mermaid_text(e.label.text, "?")}"| {dst}')
        else:
            lines.append(f"    {src} {arrow} {dst}")
    return "\n".join(lines)


def to_svg(d: Diagram, pad: float = 12.0) -> str:
    """Clean vector rendering: idealised shapes, straight connectors, label ink."""
    x0, y0, x1, y1 = d.rect
    w, h = (x1 - x0) + 2 * pad, (y1 - y0) + 2 * pad
    ox, oy = x0 - pad, y0 - pad
    out = [
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {w:.1f} {h:.1f}" '
        f'width="{w:.0f}" height="{h:.0f}">',
        '<g fill="none" stroke="black" stroke-width="1.4" stroke-linejoin="round">',
    ]

    def pt(p: Point) -> str:
        return f"{p[0] - ox:.1f},{p[1] - oy:.1f}"

    def closed(points: Sequence[Point], fill: str = "none") -> str:
        d = "M" + " L".join(pt(p) for p in points) + " Z"
        return f'<path d="{d}" fill="{fill}"/>'

    def head(tip: Point, tail: Point) -> str:
        ang = math.atan2(tip[1] - tail[1], tip[0] - tail[0])
        wing = [
            (tip[0] - 8 * math.cos(ang + da), tip[1] - 8 * math.sin(ang + da)) for da in (-0.4, 0.4)
        ]
        return closed([tip, wing[0], wing[1]], fill="black")

    for n in d.nodes:
        r = n.shape.rect
        if n.shape.kind == "rect":
            out.append(
                f'<rect x="{r[0] - ox:.1f}" y="{r[1] - oy:.1f}" width="{r[2] - r[0]:.1f}" '
                f'height="{r[3] - r[1]:.1f}" rx="3"/>'
            )
        elif n.shape.kind == "ellipse":
            out.append(
                f'<ellipse cx="{(r[0] + r[2]) / 2 - ox:.1f}" cy="{(r[1] + r[3]) / 2 - oy:.1f}" '
                f'rx="{(r[2] - r[0]) / 2:.1f}" ry="{(r[3] - r[1]) / 2:.1f}"/>'
            )
        elif n.shape.kind == "diamond":
            cx, cy = (r[0] + r[2]) / 2, (r[1] + r[3]) / 2
            pts = [(cx, r[1]), (r[2], cy), (cx, r[3]), (r[0], cy)]
            out.append(closed(pts))
        else:
            out.append(closed(n.shape.points))
    for e in d.edges:
        pts = e.shape.points
        a, b = pts[0], pts[-1]
        out.append(f'<path d="M{pt(a)} L{pt(b)}"/>')
        if e.shape.head_at_end:
            out.append(head(b, a))
        if e.shape.head_at_start:
            out.append(head(a, b))
    out.append("</g>")
    out.append('<g fill="none" stroke="#333" stroke-width="0.9" stroke-linecap="round">')
    for lab in d.labels():
        if lab.text:
            cx, cy = (lab.rect[0] + lab.rect[2]) / 2 - ox, (lab.rect[1] + lab.rect[3]) / 2 - oy
            safe = lab.text.replace("&", "&amp;").replace("<", "&lt;").replace("\n", " ")
            out.append(
                f'<text x="{cx:.1f}" y="{cy + 3:.1f}" font-family="sans-serif" font-size="9" '
                f'text-anchor="middle" fill="black" stroke="none">{safe}</text>'
            )
            continue
        for s in lab.strokes:
            out.append(f'<polyline points="{" ".join(pt(p) for p in s.points)}"/>')
    out.append("</g></svg>")
    return "\n".join(out)


def summary(d: Diagram) -> Dict[str, object]:
    def label_text(lab: Optional[Label]) -> Optional[str]:
        return lab.text if lab else None

    return {
        "nodes": [
            {
                "id": n.id,
                "shape": n.shape.kind,
                "label": label_text(n.label),
                "rect": [round(v, 1) for v in n.shape.rect],
            }
            for n in d.nodes
        ],
        "edges": [
            {
                "from": e.source,
                "to": e.target,
                "directed": e.directed,
                "label": label_text(e.label),
            }
            for e in d.edges
        ],
        "free_text": [label_text(lab) for lab in d.free_text],
    }
