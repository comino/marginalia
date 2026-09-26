"""Turn raw strokes into editorial marks anchored to the text they touch.

Pipeline for one page:

1. Per-stroke geometry features (size, straightness, retracing, closure,
   ink density).
2. Single-stroke classification against the PDF's word boxes:
   ``highlight``, ``strikethrough``, ``underline``, ``scribble`` (cross-out),
   ``circle`` (enclosure), ``margin_bar`` (vertical line beside text).
   Everything else is handwriting.
3. Handwriting strokes are clustered by proximity into ``note`` regions.
4. Each note is attached to the nearest mark (a circled phrase plus a margin
   comment becomes one request) or, failing that, to the nearest text block.

The thresholds were tuned on real device ink (fineliner, reMarkable Paper Pro)
and are expressed relative to the page's median word height where possible, so
they survive different font sizes.
"""

from __future__ import annotations

import hashlib
import math
import re
import statistics
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence, Set, Tuple

from remarkable_mcp.workflows.ink.page import PageInk, Rect, Stroke, Word

# Intent a mark implies before any handwritten note refines it.
_INTENTS = {
    "strikethrough": "delete",
    "scribble": "delete",
    "underline": "attention",
    "highlight": "attention",
    "circle": "attention",
    "margin_bar": "attention",
    "note": "comment",
}


@dataclass
class TextBlock:
    """A paragraph-like region notes and bars can anchor to."""

    id: str
    rect: Rect
    text: str = ""


@dataclass
class Mark:
    kind: str  # see _INTENTS
    page: int
    rect: Rect
    strokes: List[Stroke]
    words: List[Word] = field(default_factory=list)
    block_ids: List[str] = field(default_factory=list)
    note: Optional["Mark"] = None  # handwriting attached to this mark
    confidence: float = 0.9
    page_key: str = ""  # PageInk.key: stable across tablet page insertions

    @property
    def target_text(self) -> str:
        return " ".join(w.text for w in _reading_order(self.words))

    @property
    def intent(self) -> str:
        base = _INTENTS[self.kind]
        if self.note is not None:
            if base == "delete":
                return "replace"
            return "change"
        return base

    @property
    def id(self) -> str:
        """Stable id of the mark itself (its note may grow; see seen_keys)."""
        h = hashlib.sha1(f"{self.page_key or self.page}:{self.kind}".encode())
        for s in self.strokes:
            h.update(s.fingerprint().encode())
        return "m" + h.hexdigest()[:8]

    @property
    def stroke_fingerprints(self) -> List[str]:
        prints = [s.fingerprint() for s in self.strokes]
        if self.note is not None:
            prints += self.note.stroke_fingerprints
        return prints

    @property
    def seen_keys(self) -> List[str]:
        """One key per stroke (mark + note). A mark is new while any key is unseen,
        so a note written next to an already-collected mark comes back."""
        prefix = self.page_key or f"page{self.page}"
        return [f"{prefix}:{fp}" for fp in self.stroke_fingerprints]


# --------------------------------------------------------------------------- geometry


def _union(rects: Sequence[Rect]) -> Rect:
    return (
        min(r[0] for r in rects),
        min(r[1] for r in rects),
        max(r[2] for r in rects),
        max(r[3] for r in rects),
    )


def _inflate(r: Rect, dx: float, dy: float) -> Rect:
    return r[0] - dx, r[1] - dy, r[2] + dx, r[3] + dy


def _intersects(a: Rect, b: Rect) -> bool:
    return a[0] <= b[2] and b[0] <= a[2] and a[1] <= b[3] and b[1] <= a[3]


def _overlap_1d(a0: float, a1: float, b0: float, b1: float) -> float:
    return max(0.0, min(a1, b1) - max(a0, b0))


def _center(r: Rect) -> Tuple[float, float]:
    return (r[0] + r[2]) / 2, (r[1] + r[3]) / 2


def _contains_point(r: Rect, p: Tuple[float, float]) -> bool:
    return r[0] <= p[0] <= r[2] and r[1] <= p[1] <= r[3]


def rect_distance(a: Rect, b: Rect) -> float:
    dx = max(0.0, max(a[0], b[0]) - min(a[2], b[2]))
    dy = max(0.0, max(a[1], b[1]) - min(a[3], b[3]))
    return math.hypot(dx, dy)


def _point_in_polygon(pt: Tuple[float, float], poly: Sequence[Tuple[float, float]]) -> bool:
    """Ray casting; the polygon is implicitly closed."""
    x, y = pt
    inside = False
    j = len(poly) - 1
    for i in range(len(poly)):
        xi, yi = poly[i]
        xj, yj = poly[j]
        if (yi > y) != (yj > y) and x < (xj - xi) * (y - yi) / ((yj - yi) or 1e-9) + xi:
            inside = not inside
        j = i
    return inside


def _area(r: Rect) -> float:
    return max(0.0, r[2] - r[0]) * max(0.0, r[3] - r[1])


def _overlap_frac(a: Rect, b: Rect) -> float:
    """Share of ``a``'s area covered by ``b``."""
    inter = _overlap_1d(a[0], a[2], b[0], b[2]) * _overlap_1d(a[1], a[3], b[1], b[3])
    return inter / (_area(a) or 1e-6)


def _reading_order(words: Sequence[Word]) -> List[Word]:
    return sorted(words, key=lambda w: (round(w.rect[1] / 3), w.rect[0]))


@dataclass
class _Features:
    w: float
    h: float
    length: float
    straightness: float  # endpoint distance / path length
    closure: float  # endpoint distance / max(w, h)
    x_reversals: int
    y_reversals: int
    density: float  # path length / bbox diagonal
    inner: float  # share of path length in the central half of the bbox (fill vs. outline)
    smooth_straightness: float  # straightness after removing tremor / waves (< ~2 pt)
    swept: float  # angle (radians) the stroke sweeps around its box centre
    sagitta: float  # how far the path bows away from the chord between its ends


def _reversals(values: Sequence[float], jitter: float = 0.3) -> int:
    count, prev = 0, None
    for a, b in zip(values, values[1:]):
        d = b - a
        if abs(d) < jitter:
            continue
        sign = d > 0
        if prev is not None and sign != prev:
            count += 1
        prev = sign
    return count


def stroke_features(s: Stroke) -> _Features:
    x0, y0, x1, y1 = s.bbox
    w, h = x1 - x0, y1 - y0
    length = s.length or 1e-6
    end = math.dist(s.points[0], s.points[-1])
    diag = math.hypot(w, h) or 1e-6
    cx0, cx1 = x0 + 0.25 * w, x1 - 0.25 * w
    cy0, cy1 = y0 + 0.25 * h, y1 - 0.25 * h
    # Share of *path length* (not of sample points) in the central half of the
    # box: independent of how densely the pen sampled the stroke.
    inside = 0.0
    for (ax, ay), (bx, by) in zip(s.points, s.points[1:]):
        mx, my = (ax + bx) / 2, (ay + by) / 2
        if cx0 <= mx <= cx1 and cy0 <= my <= cy1:
            inside += math.hypot(bx - ax, by - ay)
    inner = inside / length if len(s.points) > 1 else 0.0
    simple = simplify(s.points, 2.2)
    simple_len = sum(math.dist(a, b) for a, b in zip(simple, simple[1:])) or 1e-6
    return _Features(
        w=w,
        h=h,
        length=length,
        straightness=end / length,
        closure=end / (max(w, h) or 1e-6),
        x_reversals=_reversals([p[0] for p in s.points]),
        y_reversals=_reversals([p[1] for p in s.points]),
        density=length / diag,
        inner=inner,
        smooth_straightness=end / simple_len,
        swept=_swept_angle(s.points, ((x0 + x1) / 2, (y0 + y1) / 2)),
        sagitta=_sagitta(s.points),
    )


def _sagitta(points: Sequence[Tuple[float, float]]) -> float:
    (ax, ay), (bx, by) = points[0], points[-1]
    norm = math.hypot(bx - ax, by - ay)
    if norm == 0:
        return 0.0
    return max(abs((by - ay) * px - (bx - ax) * py + bx * ay - by * ax) / norm for px, py in points)


def simplify(points: Sequence[Tuple[float, float]], epsilon: float) -> List[Tuple[float, float]]:
    """Ramer-Douglas-Peucker (iterative): drops wiggles smaller than ``epsilon``."""
    if len(points) < 3:
        return list(points)
    keep = [False] * len(points)
    keep[0] = keep[-1] = True
    stack = [(0, len(points) - 1)]
    while stack:
        a, b = stack.pop()
        (ax, ay), (bx, by) = points[a], points[b]
        dx, dy = bx - ax, by - ay
        norm = math.hypot(dx, dy)
        best, idx = 0.0, -1
        for i in range(a + 1, b):
            px, py = points[i]
            d = (
                abs(dy * px - dx * py + bx * ay - by * ax) / norm
                if norm
                else math.hypot(px - ax, py - ay)
            )
            if d > best:
                best, idx = d, i
        if best > epsilon and idx > 0:
            keep[idx] = True
            stack += [(a, idx), (idx, b)]
    return [p for p, k in zip(points, keep) if k]


def _swept_angle(points: Sequence[Tuple[float, float]], centre: Tuple[float, float]) -> float:
    """Total angle the path turns around ``centre`` (2*pi for a full loop)."""
    total, prev = 0.0, None
    for x, y in points:
        a = math.atan2(y - centre[1], x - centre[0])
        if prev is not None:
            d = a - prev
            while d > math.pi:
                d -= 2 * math.pi
            while d < -math.pi:
                d += 2 * math.pi
            total += d
        prev = a
    return abs(total)


# --------------------------------------------------------------------------- classification


def visual_lines(words: Sequence[Word]) -> Dict[int, int]:
    """Map id(word) -> visual line number, grouping words by vertical centre.

    PyMuPDF's own block/line numbers follow the PDF's text objects, which for
    many generators (including PyMuPDF's Story layout) split one visual line
    into several blocks or merge several lines into one; geometry is reliable.
    """
    order = sorted(words, key=lambda w: (w.rect[1] + w.rect[3]) / 2)
    out: Dict[int, int] = {}
    line, anchor = -1, None
    for w in order:
        cy = (w.rect[1] + w.rect[3]) / 2
        h = w.rect[3] - w.rect[1]
        if anchor is None or abs(cy - anchor) > 0.35 * h:
            line += 1
            anchor = cy
        out[id(w)] = line
    return out


def _word_height(words: Sequence[Word]) -> float:
    if not words:
        return 10.0
    return statistics.median(w.rect[3] - w.rect[1] for w in words)


def _words_in(words: Sequence[Word], r: Rect, min_frac: float = 0.5) -> List[Word]:
    """Words whose area lies at least ``min_frac`` inside ``r``."""
    out = []
    for w in words:
        wx = _overlap_1d(w.rect[0], w.rect[2], r[0], r[2])
        wy = _overlap_1d(w.rect[1], w.rect[3], r[1], r[3])
        area = (w.rect[2] - w.rect[0]) * (w.rect[3] - w.rect[1]) or 1e-6
        if wx * wy / area >= min_frac:
            out.append(w)
    return out


# Fallback for words without font metrics (rotated pages): where the baseline
# and the x-height middle sit inside a PyMuPDF word box (ascender..descender).
_BASELINE = 0.80
_XMID = 0.55
# With real font metrics (fractions of the font size): a stroke above
# baseline - _STRIKE_FLOOR*size (and not above the x-height) strikes through; a
# stroke from there down to _UNDER_CEIL*size below the baseline underlines.
_STRIKE_FLOOR = 0.08
_STRIKE_TOP = 0.6
_UNDER_CEIL = 0.6


def _line_geometry(line_words: Sequence[Word]) -> Tuple[float, float, bool]:
    """(baseline y, font size or box height, whether real metrics were used)."""
    metric = [w for w in line_words if w.baseline is not None and w.size]
    if metric:
        return (
            statistics.median(w.baseline for w in metric),
            statistics.median(w.size for w in metric),
            True,
        )
    top = statistics.median(w.rect[1] for w in line_words)
    h = statistics.median(w.rect[3] - w.rect[1] for w in line_words) or 1e-6
    return top + _BASELINE * h, h, False


def _covered_words(words: Sequence[Word], s: Stroke, word_h: float) -> List[Word]:
    """Words a horizontal stroke spans, ignoring the overshoot at its ends.

    People start and end strikes a little beyond the phrase; trimming the
    stroke's ends keeps a short neighbouring word ("a", "to") out of it.
    """
    x0, _, x1, _ = s.bbox
    trim = min(0.06 * (x1 - x0), 0.45 * word_h)
    x0, x1 = x0 + trim, x1 - trim
    out = []
    for w in words:
        ww = (w.rect[2] - w.rect[0]) or 1e-6
        if _overlap_1d(w.rect[0], w.rect[2], x0, x1) / ww >= 0.5:
            out.append(w)
    return out


def _horizontal_target(
    words: Sequence[Word], s: Stroke, word_h: float = 12.0
) -> Optional[Tuple[str, List[Word]]]:
    """Decide whether a horizontal stroke strikes through or underlines a line.

    Uses each line's real baseline and font size when known: above the
    baseline (inside the x-height) is a strike, at or below it an underline.
    Picks the single line the stroke belongs to.
    """
    ys = sorted(p[1] for p in s.points)
    y = ys[len(ys) // 2]
    line_of = visual_lines(words)
    lines: Dict[int, List[Word]] = {}
    for w in _covered_words(words, s, word_h):
        lines.setdefault(line_of[id(w)], []).append(w)
    best: Optional[Tuple[float, str, List[Word]]] = None
    for line_words in lines.values():
        base, size, metric = _line_geometry(line_words)
        if metric:
            rel = (base - y) / size  # > 0: above the baseline
            if _STRIKE_FLOOR < rel <= _STRIKE_TOP:
                cand = (abs(rel - 0.27), "strikethrough")
            elif -_UNDER_CEIL <= rel <= _STRIKE_FLOOR:
                cand = (abs(rel + 0.1), "underline")
            else:
                continue
        else:
            top = base - _BASELINE * size
            strike = abs(y - (top + _XMID * size)) / size
            under = (y - base) / size
            if strike <= 0.2:
                cand = (strike, "strikethrough")
            elif -0.12 <= under <= 0.45:
                cand = (abs(under), "underline")
            else:
                continue
        if best is None or cand[0] < best[0]:
            best = (cand[0], cand[1], line_words)
    if best is None:
        return None
    return best[1], best[2]


def _x_band(w: Word) -> Tuple[float, float]:
    """Vertical extent of the letters' x-height (where a cross-out must pass)."""
    if w.baseline is not None and w.size:
        return w.baseline - 0.5 * w.size, w.baseline
    h = w.rect[3] - w.rect[1]
    return w.rect[1] + 0.35 * h, w.rect[1] + 0.8 * h


def _scribble_targets(words: Sequence[Word], rect: Rect, points) -> List[Word]:
    """Words a cross-out really covers: its ink reaches the words' x-height."""
    covered = _words_in(words, rect, 0.35)
    hits = []
    for w in covered:
        lo, hi = _x_band(w)
        mid = (lo + hi) / 2
        if rect[1] <= mid <= rect[3]:
            hits.append(w)
    return hits


def _swiped_words(words: Sequence[Word], s: Stroke) -> List[Word]:
    """Words a highlighter swipe passes through (centre line inside the word box)."""
    ys = sorted(p[1] for p in s.points)
    y = ys[len(ys) // 2]
    x0, _, x1, _ = s.bbox
    hits = [
        w
        for w in words
        if w.rect[1] <= y <= w.rect[3]
        and _overlap_1d(w.rect[0], w.rect[2], x0, x1) / ((w.rect[2] - w.rect[0]) or 1e-6) >= 0.5
    ]
    if not hits:
        return hits
    best = min(hits, key=lambda w: abs(y - (w.rect[1] + w.rect[3]) / 2))
    line_of = visual_lines(words)
    return [w for w in hits if line_of[id(w)] == line_of[id(best)]]


_NUMBER = re.compile(r"^\(?\d{1,3}[.)]?$")


def classify_stroke(s: Stroke, words: Sequence[Word], word_h: float) -> Tuple[str, List[Word]]:
    """Classify one stroke; returns (kind, words it targets). kind 'ink' = handwriting."""
    f = stroke_features(s)
    bbox = s.bbox

    if s.is_highlighter:
        return "highlight", _swiped_words(words, s)

    reversals = f.x_reversals + f.y_reversals
    # Cross-out: back-and-forth ink that fills (not outlines) its box and
    # reaches the words' x-height (writing *between* lines does not).
    if (
        max(f.w, f.h) > 1.0 * word_h
        and reversals >= 6
        and (f.density > 4.0 or (f.density > 1.2 and f.inner > 0.1))
        and (f.inner > 0.1 or reversals >= 20)
    ):
        covered = _scribble_targets(words, bbox, s.points)
        if covered:
            return "scribble", covered

    # Horizontal line (tremor and waves smoothed out): underline or strike.
    # A wavy line is still a line: a flat band that never doubles back.
    band = f.h < 0.35 * word_h and f.x_reversals <= 2
    if (
        f.w > 4 * max(f.h, 1.0)
        and f.w > 0.5 * word_h
        and (f.smooth_straightness > 0.85 or band)
        and f.sagitta < 0.4 * word_h  # an arc (half a circle) bows far more
        and abs(math.atan2(f.h, f.w)) < 0.35
    ):
        hit = _horizontal_target(words, s, word_h)
        if hit:
            return hit

    # Enclosure around text: ink runs along the outline, sweeping (almost) a
    # full turn - also a loop that is not quite closed, or drawn past its start.
    if (
        f.inner < 0.08
        and f.swept >= 1.55 * math.pi
        and f.length > 1.8 * max(f.w, f.h)
        and min(f.w, f.h) > 0.8 * word_h
    ):
        inside = [w for w in words if _point_in_polygon(_center(w.rect), s.points)]
        if inside:
            return "circle", inside

    # Vertical bar beside text (often retraced), not crossing any word, with
    # printed text close by on the lines it spans.
    if f.h > 3 * max(f.w, 1.0) and f.h > 0.75 * word_h and f.w < word_h:
        if not _words_in(words, _inflate(bbox, 1, 0), 0.15) and any(
            bbox[1] - 2 <= _center(w.rect)[1] <= bbox[3] + 2
            and rect_distance(bbox, w.rect) <= 4 * word_h
            for w in words
        ):
            return "margin_bar", []

    # Long straight rule that marks no text: a divider or a line under
    # handwriting. Kept out of note clustering so it cannot chain notes.
    if f.smooth_straightness > 0.9 and f.length > 6 * word_h and f.sagitta < 0.4 * word_h:
        return "rule", []

    return "ink", []


def _bar_targets(bar: Rect, words: Sequence[Word], word_h: float) -> List[Word]:
    """Words on the lines a margin bar spans, in the text column next to it.

    Takes the nearer side of the bar, then walks each line outward from the
    bar and stops at the first wide gap, so a neighbouring column is excluded.
    """
    y0, y1 = bar[1] - 2, bar[3] + 2
    on_lines = [w for w in words if y0 <= _center(w.rect)[1] <= y1]
    # A bar in the gutter often sits next to a paragraph/list number: that
    # number is not what the bar marks - skip it and look at the text.
    on_lines = [w for w in on_lines if not _NUMBER.match(w.text)] or on_lines
    if not on_lines:
        return []
    bx = (bar[0] + bar[2]) / 2
    right = [w for w in on_lines if w.rect[0] >= bx]
    left = [w for w in on_lines if w.rect[2] <= bx]
    gap_r = min((w.rect[0] - bx for w in right), default=math.inf)
    gap_l = min((bx - w.rect[2] for w in left), default=math.inf)
    side, outward = (right, 1) if gap_r <= gap_l else (left, -1)
    line_of = visual_lines(side)
    by_line: Dict[int, List[Word]] = {}
    for w in side:
        by_line.setdefault(line_of[id(w)], []).append(w)
    picked: List[Word] = []
    max_gap = 1.2 * word_h  # word spacing is ~0.3 word_h; column gutters are wider

    for line_words in by_line.values():
        line_words.sort(key=lambda w: w.rect[0] * outward)
        edge = bx
        for w in line_words:
            near_edge = w.rect[0] if outward > 0 else w.rect[2]
            if abs(near_edge - edge) > max_gap and edge != bx:
                break
            if edge == bx and abs(near_edge - bx) > 4 * word_h:
                break
            picked.append(w)
            edge = w.rect[2] if outward > 0 else w.rect[0]
    return picked


# --------------------------------------------------------------------------- clustering


def _cluster(rects: List[Rect], dx: float, dy: float) -> List[List[int]]:
    """Union-find clustering of rects that touch after inflating by (dx, dy)."""
    grown = [_inflate(r, dx, dy) for r in rects]
    return _cluster_by(len(rects), lambda i, j: _intersects(grown[i], grown[j]))


def _cluster_by(n: int, linked) -> List[List[int]]:
    """Union-find over ``n`` items joined whenever ``linked(i, j)`` is true."""
    parent = list(range(n))

    def find(i: int) -> int:
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    for i in range(n):
        for j in range(i + 1, n):
            if linked(i, j):
                parent[find(i)] = find(j)
    groups: Dict[int, List[int]] = {}
    for i in range(n):
        groups.setdefault(find(i), []).append(i)
    return list(groups.values())


def default_blocks(page: PageInk) -> List[TextBlock]:
    """Text blocks from the PDF's own layout (used when there is no manifest)."""
    by_block: Dict[int, List[Word]] = {}
    for w in page.words:
        by_block.setdefault(w.block, []).append(w)
    blocks = []
    for n, ws in sorted(by_block.items()):
        blocks.append(
            TextBlock(
                id=f"b{page.page}.{n}",
                rect=_union([w.rect for w in ws]),
                text=" ".join(w.text for w in _reading_order(ws)),
            )
        )
    return blocks


def _nearest_block(r: Rect, blocks: Sequence[TextBlock]) -> Optional[TextBlock]:
    """Block beside ``r`` (same vertical band) if any, else the closest one."""
    if not blocks:
        return None
    cy = _center(r)[1]
    beside = [b for b in blocks if b.rect[1] - 4 <= cy <= b.rect[3] + 4]
    pool = beside or list(blocks)
    return min(pool, key=lambda b: rect_distance(r, b.rect))


def _blocks_for_words(words: Sequence[Word], blocks: Sequence[TextBlock]) -> List[str]:
    ids: List[str] = []
    for w in words:
        c = _center(w.rect)
        hit = next((b for b in blocks if _contains_point(_inflate(b.rect, 1, 1), c)), None)
        if hit and hit.id not in ids:
            ids.append(hit.id)
    return ids


def analyze_page(page: PageInk, blocks: Optional[Sequence[TextBlock]] = None) -> List[Mark]:
    """Classify, cluster and anchor all ink on one page."""
    blocks = list(blocks) if blocks is not None else default_blocks(page)
    words = page.words
    word_h = _word_height(words)

    marks: List[Mark] = []
    ink: List[Stroke] = []
    for s in page.strokes:
        kind, targets = classify_stroke(s, words, word_h)
        if kind == "ink":
            ink.append(s)
        elif kind == "rule":
            continue
        else:
            marks.append(Mark(kind=kind, page=page.page, rect=s.bbox, strokes=[s], words=targets))

    # Two arcs that meet end to end around text are one circle (a loop drawn
    # in two strokes); test the joined path before treating them as writing.
    joined, ink = _join_arcs(ink, words, word_h, page.page)
    marks += joined

    # A "margin bar" hugging handwriting is a letter (l, 1, !), not a bar.
    kept: List[Mark] = []
    for m in marks:
        if m.kind == "margin_bar" and any(
            rect_distance(m.rect, s.bbox) < 0.5 * word_h for s in ink
        ):
            ink.append(m.strokes[0])
        else:
            kept.append(m)
    marks = kept

    # A loop inside a cross-out is part of the cross-out.
    scribbles = [m for m in marks if m.kind == "scribble"]
    if scribbles:
        for m in marks:
            if m.kind == "circle" and any(_overlap_frac(m.rect, sc.rect) > 0.5 for sc in scribbles):
                m.kind = "scribble"

    # Merge same-kind marks that are really one gesture (double bars, a
    # strikethrough drawn in two pulls, a scribble in several strokes).
    marks = _merge_marks(marks, word_h, words)
    for m in marks:
        if m.kind == "margin_bar":
            m.words = _bar_targets(m.rect, words, word_h)
        m.block_ids = _blocks_for_words(m.words, blocks) or (
            [b.id] if (b := _nearest_block(m.rect, blocks)) else []
        )

    # Native text highlights (highlighter tool snapped to PDF text).
    for hl in page.highlights:
        rect = _union(hl.rects)
        hl_words = [
            w
            for w in words
            if any(_contains_point(_inflate(r, 1, 1), _center(w.rect)) for r in hl.rects)
        ]
        pseudo = Stroke(
            index=-1,
            points=[(rect[0], rect[1]), (rect[2], rect[3])],
            tool="highlighter",
            color=hl.color,
            width=0,
        )
        m = Mark(kind="highlight", page=page.page, rect=rect, strokes=[pseudo], words=hl_words)
        m.block_ids = _blocks_for_words(hl_words, blocks)
        marks.append(m)

    # Handwriting -> notes.
    notes: List[Mark] = []
    if ink:
        groups = _cluster([s.bbox for s in ink], dx=1.0 * word_h, dy=0.6 * word_h)
        for g in groups:
            strokes = sorted((ink[i] for i in g), key=lambda s: s.index)
            rect = _union([s.bbox for s in strokes])
            notes.append(
                Mark(kind="note", page=page.page, rect=rect, strokes=strokes, confidence=0.7)
            )

    # Attach each note to a mark: the closest one within reach, or one on the
    # same lines when the note sits in the margin beside it. Else it is a
    # standalone comment on the nearest text block.
    reach = 3.0 * word_h
    standalone: List[Mark] = []
    for note in notes:
        candidates = [m for m in marks if m.note is None]
        near = [m for m in candidates if rect_distance(m.rect, note.rect) <= reach]
        home = _nearest_block(note.rect, blocks)
        beside = [
            m
            for m in candidates
            if home is not None
            and home.id in m.block_ids
            and _overlap_1d(
                m.rect[1] - 0.5 * word_h, m.rect[3] + 0.5 * word_h, note.rect[1], note.rect[3]
            )
            > 0
        ]
        best = min(near or beside, key=lambda m: rect_distance(m.rect, note.rect), default=None)
        if best is not None:
            best.note = note
            continue
        note.words = _words_in(words, note.rect, 0.5)
        blk = _nearest_block(note.rect, blocks)
        note.block_ids = [blk.id] if blk else []
        standalone.append(note)

    result = marks + standalone
    for m in result:
        m.page_key = page.key
        if m.note is not None:
            m.note.page_key = page.key
    result.sort(key=lambda m: (m.rect[1], m.rect[0]))
    return result


def _join_arcs(
    ink: List[Stroke], words: Sequence[Word], word_h: float, page: int
) -> Tuple[List[Mark], List[Stroke]]:
    """Pair curved strokes whose ends meet and test the joined path as a circle."""

    def is_arc(s: Stroke) -> bool:
        f = stroke_features(s)
        return f.smooth_straightness < 0.9 or f.sagitta > 0.3 * word_h

    arcs = [s for s in ink if s.length > 1.5 * word_h and is_arc(s)]
    used: set = set()
    out: List[Mark] = []
    reach = 0.8 * word_h
    for i, a in enumerate(arcs):
        if id(a) in used:
            continue
        for b in arcs[i + 1 :]:
            if id(b) in used:
                continue
            best = None
            for pa in (a.points, a.points[::-1]):
                for pb in (b.points, b.points[::-1]):
                    gap = math.dist(pa[-1], pb[0])
                    if gap <= reach and (best is None or gap < best[0]):
                        best = (gap, pa + pb)
            if best is None:
                continue
            joined = Stroke(
                index=a.index, points=best[1], tool=a.tool, color=a.color, width=a.width
            )
            kind, targets = classify_stroke(joined, words, word_h)
            if kind == "circle":
                used.update((id(a), id(b)))
                out.append(
                    Mark(kind="circle", page=page, rect=joined.bbox, strokes=[a, b], words=targets)
                )
                break
    return out, [s for s in ink if id(s) not in used]


def _unique_words(parts: Sequence[Mark]) -> List[Word]:
    """The parts' words in order, each once (a hashed key: comparing
    dataclasses with ``==`` in a list is quadratic and slow on dense pages)."""
    seen: Set[Tuple[str, Tuple[float, ...]]] = set()
    words: List[Word] = []
    for p in parts:
        for w in p.words:
            key = (w.text, tuple(w.rect))
            if key not in seen:
                seen.add(key)
                words.append(w)
    return words


def _merge_marks(marks: List[Mark], word_h: float, all_words: Sequence[Word] = ()) -> List[Mark]:
    out: List[Mark] = []
    line_of: Optional[Dict[int, int]] = None  # computed once, when a join needs it
    in_order: List[Word] = []
    for kind in dict.fromkeys(m.kind for m in marks):
        same = [m for m in marks if m.kind == kind]
        if kind in ("strikethrough", "underline", "highlight"):
            # One line drawn in several pulls: same text line, small gaps.
            rects = [m.rect for m in same]

            def same_line(i: int, j: int) -> bool:
                cy_i = (rects[i][1] + rects[i][3]) / 2
                cy_j = (rects[j][1] + rects[j][3]) / 2
                gap = max(rects[i][0], rects[j][0]) - min(rects[i][2], rects[j][2])
                return abs(cy_i - cy_j) <= 0.4 * word_h and gap <= 0.8 * word_h

            for g in _cluster_by(len(same), same_line):
                parts = sorted((same[i] for i in g), key=lambda m: m.rect[0])
                words = _unique_words(parts)
                if len(parts) > 1 and words and all_words:
                    # Re-derive the words over the joined span: a word the
                    # pulls met in the middle of belongs to neither half alone.
                    if line_of is None:
                        line_of = visual_lines(all_words)
                        in_order = _reading_order(all_words)
                    lines = {line_of[id(w)] for w in words}
                    x0 = min(p.rect[0] for p in parts)
                    x1 = max(p.rect[2] for p in parts)
                    trim = min(0.06 * (x1 - x0), 0.45 * word_h)
                    words = [
                        w
                        for w in in_order
                        if line_of[id(w)] in lines
                        and _overlap_1d(w.rect[0], w.rect[2], x0 + trim, x1 - trim)
                        >= 0.5 * ((w.rect[2] - w.rect[0]) or 1e-6)
                    ]
                out.append(
                    Mark(
                        kind=kind,
                        page=parts[0].page,
                        rect=_union([p.rect for p in parts]),
                        strokes=[s for p in parts for s in p.strokes],
                        words=words,
                    )
                )
            continue
        if kind in ("margin_bar", "scribble", "circle"):
            dx, dy = {
                "margin_bar": (0.6 * word_h, 0.5 * word_h),
                "scribble": (0.5 * word_h, 0.2 * word_h),
                "circle": (0.0, 0.0),
            }[kind]
            rects = [m.rect for m in same]
            if kind == "circle":
                # Loops drawn twice around the same text: merge on heavy overlap only.
                groups = _cluster_by(
                    len(same),
                    lambda i, j: max(
                        _overlap_frac(rects[i], rects[j]), _overlap_frac(rects[j], rects[i])
                    )
                    > 0.6,
                )
            else:
                groups = _cluster(rects, dx, dy)
            for g in groups:
                parts = [same[i] for i in g]
                words = _unique_words(parts)
                out.append(
                    Mark(
                        kind=kind,
                        page=parts[0].page,
                        rect=_union([p.rect for p in parts]),
                        strokes=[s for p in parts for s in p.strokes],
                        words=words,
                    )
                )
        else:
            out.extend(same)
    return out
