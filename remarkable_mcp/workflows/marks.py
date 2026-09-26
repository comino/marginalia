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
import statistics
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence, Tuple

from remarkable_mcp.workflows.ink import PageInk, Rect, Stroke, Word

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
    inner: float  # share of points in the central half of the bbox (fill vs. outline)


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
    inner = sum(1 for x, y in s.points if cx0 <= x <= cx1 and cy0 <= y <= cy1) / len(s.points)
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
    )


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


# Where the baseline and the x-height middle sit inside a PyMuPDF word box
# (which spans ascender to descender), as fractions of the box height.
_BASELINE = 0.78
_XMID = 0.52


def _horizontal_target(words: Sequence[Word], s: Stroke) -> Optional[Tuple[str, List[Word]]]:
    """Decide whether a horizontal stroke strikes through or underlines a line.

    Scores every text line the stroke spans by how close the stroke's median y
    is to that line's baseline (underline) or x-height middle (strike), in
    units of line height, and keeps the single best line.
    """
    ys = sorted(p[1] for p in s.points)
    y = ys[len(ys) // 2]
    x0, _, x1, _ = s.bbox
    line_of = visual_lines(words)
    lines: Dict[int, List[Word]] = {}
    for w in words:
        ww = (w.rect[2] - w.rect[0]) or 1e-6
        if _overlap_1d(w.rect[0], w.rect[2], x0, x1) / ww >= 0.5:
            lines.setdefault(line_of[id(w)], []).append(w)
    best: Optional[Tuple[float, str, List[Word]]] = None
    for line_words in lines.values():
        top = statistics.median(w.rect[1] for w in line_words)
        h = statistics.median(w.rect[3] - w.rect[1] for w in line_words) or 1e-6
        strike = abs(y - (top + _XMID * h)) / h
        under = (y - (top + _BASELINE * h)) / h
        for score, kind, ok in (
            (strike, "strikethrough", strike <= 0.2),
            (abs(under), "underline", -0.12 <= under <= 0.45),
        ):
            if ok and (best is None or score < best[0]):
                best = (score, kind, line_words)
    if best is None:
        return None
    return best[1], best[2]


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


def classify_stroke(s: Stroke, words: Sequence[Word], word_h: float) -> Tuple[str, List[Word]]:
    """Classify one stroke; returns (kind, words it targets). kind 'ink' = handwriting."""
    f = stroke_features(s)
    bbox = s.bbox

    if s.is_highlighter:
        return "highlight", _swiped_words(words, s)

    # Cross-out: dense back-and-forth ink that fills (not outlines) its box.
    if (
        f.density > 4.0
        and max(f.w, f.h) > 1.5 * word_h
        and f.x_reversals + f.y_reversals >= 6
        and (f.inner > 0.1 or f.x_reversals + f.y_reversals >= 20)
    ):
        covered = _words_in(words, bbox, 0.35)
        if covered:
            return "scribble", covered

    # Horizontal line: underline or strikethrough depending on where it sits.
    if f.w > 4 * max(f.h, 1.0) and f.w > 1.5 * word_h and f.straightness > 0.75:
        hit = _horizontal_target(words, s)
        if hit:
            return hit

    # Enclosure around text: ink runs along the outline (possibly looping
    # twice), the path is at least a perimeter long, and words sit inside.
    if (
        f.inner < 0.08
        and (f.closure < 0.35 or f.length > 2.6 * max(f.w, f.h))
        and f.length > 1.8 * max(f.w, f.h)
        and min(f.w, f.h) > 0.8 * word_h
    ):
        inside = [w for w in words if _point_in_polygon(_center(w.rect), s.points)]
        if inside:
            return "circle", inside

    # Vertical bar beside text (often retraced), not crossing any word, with
    # printed text close by on the lines it spans.
    if f.h > 3 * max(f.w, 1.0) and f.h > 1.2 * word_h and f.w < word_h:
        if not _words_in(words, _inflate(bbox, 1, 0), 0.15) and any(
            bbox[1] - 2 <= _center(w.rect)[1] <= bbox[3] + 2
            and rect_distance(bbox, w.rect) <= 4 * word_h
            for w in words
        ):
            return "margin_bar", []

    # Long straight rule that marks no text: a divider or a line under
    # handwriting. Kept out of note clustering so it cannot chain notes.
    if f.straightness > 0.9 and f.length > 6 * word_h:
        return "rule", []

    return "ink", []


def _bar_targets(bar: Rect, words: Sequence[Word], word_h: float) -> List[Word]:
    """Words on the lines a margin bar spans, in the text column next to it.

    Takes the nearer side of the bar, then walks each line outward from the
    bar and stops at the first wide gap, so a neighbouring column is excluded.
    """
    y0, y1 = bar[1] - 2, bar[3] + 2
    on_lines = [w for w in words if y0 <= _center(w.rect)[1] <= y1]
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
    marks = _merge_marks(marks, word_h)
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


def _merge_marks(marks: List[Mark], word_h: float) -> List[Mark]:
    out: List[Mark] = []
    for kind in dict.fromkeys(m.kind for m in marks):
        same = [m for m in marks if m.kind == kind]
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
                words: List[Word] = []
                for p in parts:
                    words += [w for w in p.words if w not in words]
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
