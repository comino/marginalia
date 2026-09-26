"""Hand-drawn tables: grid lines -> rows x columns, handwriting per cell.

A table is recognised from its rules: long straight horizontal and vertical
strokes (a box drawn around the table counts too - its sides become rules).
Rules are clustered into row and column boundaries; every other stroke is
writing and lands in the cell containing its centre. Transcription happens
per cell, so each crop is small and unambiguous.
"""

from __future__ import annotations

import csv
import io
import math
from dataclasses import dataclass, field
from typing import List, Optional, Sequence, Tuple

from remarkable_mcp.workflows.ink import Rect, Stroke
from remarkable_mcp.workflows.marks import _union
from remarkable_mcp.workflows.sketch import classify_outline, rect_distance

MIN_RULE = 36.0  # points; shorter straight strokes are writing (dashes, t-bars)


@dataclass
class Table:
    rows: List[float]  # y boundaries, top to bottom
    cols: List[float]  # x boundaries, left to right
    cells: List[List[List[Stroke]]] = field(default_factory=list)  # [row][col] -> strokes
    rect: Rect = (0, 0, 0, 0)
    text: List[List[Optional[str]]] = field(default_factory=list)

    @property
    def shape(self) -> Tuple[int, int]:
        return len(self.rows) - 1, len(self.cols) - 1

    def cell_rect(self, r: int, c: int) -> Rect:
        return self.cols[c], self.rows[r], self.cols[c + 1], self.rows[r + 1]

    def to_markdown(self) -> str:
        grid = [
            [(t or "").replace("\n", " ").replace("|", "\\|") for t in row] for row in self.text
        ]
        if not grid:
            return ""
        head, body = grid[0], grid[1:]
        lines = ["| " + " | ".join(head) + " |", "|" + "---|" * len(head)]
        lines += ["| " + " | ".join(row) + " |" for row in body]
        return "\n".join(lines)

    def to_csv(self) -> str:
        buf = io.StringIO()
        csv.writer(buf).writerows([[t or "" for t in row] for row in self.text])
        return buf.getvalue()


def _orientation(s: Stroke) -> Optional[str]:
    x0, y0, x1, y1 = s.bbox
    w, h = x1 - x0, y1 - y0
    straight = math.dist(s.points[0], s.points[-1]) / (s.length or 1e-6)
    if straight < 0.9 or max(w, h) < MIN_RULE:
        return None
    if w > 6 * max(h, 1.0):
        return "h"
    if h > 6 * max(w, 1.0):
        return "v"
    return None


def _cluster_positions(values: Sequence[float], tol: float) -> List[float]:
    out: List[List[float]] = []
    for v in sorted(values):
        if out and v - out[-1][-1] <= tol:
            out[-1].append(v)
        else:
            out.append([v])
    return [sum(g) / len(g) for g in out]


def find_table(strokes: Sequence[Stroke], region: Optional[Rect] = None) -> Optional[Table]:
    pool = [s for s in strokes if not s.is_highlighter and len(s.points) >= 2]
    if region is not None:
        pool = [s for s in pool if rect_distance(s.bbox, region) == 0]
    h_rules: List[Tuple[float, float, float]] = []  # (y, x0, x1)
    v_rules: List[Tuple[float, float, float]] = []  # (x, y0, y1)
    writing: List[Stroke] = []
    for s in pool:
        o = _orientation(s)
        x0, y0, x1, y1 = s.bbox
        if o == "h":
            h_rules.append(((y0 + y1) / 2, x0, x1))
            continue
        if o == "v":
            v_rules.append(((x0 + x1) / 2, y0, y1))
            continue
        if max(x1 - x0, y1 - y0) >= 3 * MIN_RULE:
            shape = classify_outline(list(s.points), [s])
            if shape is not None and shape.kind == "rect":
                h_rules += [(y0, x0, x1), (y1, x0, x1)]
                v_rules += [(x0, y0, y1), (x1, y0, y1)]
                continue
        writing.append(s)
    if len(h_rules) < 2 or len(v_rules) < 2:
        return None
    ys = _cluster_positions([r[0] for r in h_rules], 7.0)
    xs = _cluster_positions([r[0] for r in v_rules], 7.0)
    if len(ys) < 2 or len(xs) < 2:
        return None
    # Rules must span most of the table to count (ignores stray long strokes).
    span_x, span_y = xs[-1] - xs[0], ys[-1] - ys[0]
    ys = [
        y for y in ys if any(abs(r[0] - y) <= 7 and (r[2] - r[1]) >= 0.5 * span_x for r in h_rules)
    ]
    xs = [
        x for x in xs if any(abs(r[0] - x) <= 7 and (r[2] - r[1]) >= 0.5 * span_y for r in v_rules)
    ]
    if len(ys) < 2 or len(xs) < 2:
        return None
    table = Table(rows=ys, cols=xs, rect=(xs[0], ys[0], xs[-1], ys[-1]))
    nr, nc = table.shape
    table.cells = [[[] for _ in range(nc)] for _ in range(nr)]
    for s in writing:
        cx = (s.bbox[0] + s.bbox[2]) / 2
        cy = (s.bbox[1] + s.bbox[3]) / 2
        r = next((i for i in range(nr) if ys[i] <= cy <= ys[i + 1]), None)
        c = next((j for j in range(nc) if xs[j] <= cx <= xs[j + 1]), None)
        if r is not None and c is not None:
            table.cells[r][c].append(s)
    table.text = [[None] * nc for _ in range(nr)]
    return table


def cell_crops(table: Table) -> List[Tuple[int, int, List[Stroke], Rect]]:
    out = []
    for r, row in enumerate(table.cells):
        for c, strokes in enumerate(row):
            if strokes:
                out.append((r, c, strokes, _union([s.bbox for s in strokes])))
    return out
