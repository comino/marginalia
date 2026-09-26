"""Agent Inbox: handwritten requests on the tablet become tasks for an agent.

The user writes requests in an "Agent Inbox" document (a ruled PDF the server
generates, or any notebook). Each request is a block of handwriting separated
from the next by an empty line. Segmentation works on stroke geometry only:

1. strokes -> text lines (vertical overlap of stroke boxes)
2. lines   -> entries (vertical gap between lines > ``ENTRY_GAP`` x line pitch)

A long straight stroke through an entry cancels it (the user struck it out).
Entry identity survives edits: a re-scan matches entries to known ones by the
overlap of stroke fingerprints, so adding words to a request updates it
instead of creating a new one.
"""

from __future__ import annotations

import hashlib
import math
import statistics
from dataclasses import dataclass, field
from datetime import datetime
from typing import Dict, Iterable, List, Optional, Sequence, Set

import pymupdf

from remarkable_mcp.workflows.ink import PageInk, Rect, Stroke
from remarkable_mcp.workflows.marks import _union, stroke_features
from remarkable_mcp.workflows.review_pdf import PAGE_H, PAGE_W

LINE_PITCH = 28.0  # ruled line spacing of the generated inbox, in points
ENTRY_GAP = 0.9  # blank space (in line pitches) that separates two entries
TEMPLATE_TOP = 64.0
TEMPLATE_MARGIN = 34.0


def render_inbox_template(pages: int = 12, title: str = "Agent Inbox") -> bytes:
    """A ruled, tablet-shaped PDF with short instructions on the first page."""
    doc = pymupdf.open()
    grey = (0.55, 0.55, 0.55)
    rule = (0.8, 0.8, 0.8)
    for n in range(pages):
        page = doc.new_page(width=PAGE_W, height=PAGE_H)
        page.insert_text((TEMPLATE_MARGIN, 30), title, fontsize=11, fontname="hebo")
        if n == 0:
            page.insert_text(
                (TEMPLATE_MARGIN, 44),
                "One request per block. Leave an empty line between requests. "
                "Strike a request through to cancel it. #tags route it.",
                fontsize=6.5,
                fontname="helv",
                color=grey,
            )
        page.insert_text(
            (PAGE_W - 40, PAGE_H - 14), f"{n + 1}", fontsize=6.5, fontname="helv", color=grey
        )
        y = TEMPLATE_TOP + LINE_PITCH
        while y < PAGE_H - 30:
            page.draw_line(
                (TEMPLATE_MARGIN, y), (PAGE_W - TEMPLATE_MARGIN, y), color=rule, width=0.4
            )
            y += LINE_PITCH
    pdf = doc.tobytes(garbage=3, deflate=True)
    doc.close()
    return pdf


@dataclass
class Entry:
    page: int
    strokes: List[Stroke]
    cancelled: bool = False
    fingerprints: Set[str] = field(default_factory=set)

    @property
    def rect(self) -> Rect:
        return _union([s.bbox for s in self.strokes])

    @property
    def id(self) -> str:
        h = hashlib.sha1()
        for fp in sorted(self.fingerprints):
            h.update(fp.encode())
        return "e" + h.hexdigest()[:8]


def _thickness(points) -> float:
    """Spread of the ink across its main direction (least-squares line).

    A (wavy) strike is a thin band; a handwritten word is as thick as its
    x-height, even though it also runs from left to right.
    """
    n = len(points)
    mx = sum(p[0] for p in points) / n
    my = sum(p[1] for p in points) / n
    sxx = sum((p[0] - mx) ** 2 for p in points)
    syy = sum((p[1] - my) ** 2 for p in points)
    sxy = sum((p[0] - mx) * (p[1] - my) for p in points)
    theta = 0.5 * math.atan2(2 * sxy, sxx - syy)
    nx, ny = -math.sin(theta), math.cos(theta)
    d = [(p[0] - mx) * nx + (p[1] - my) * ny for p in points]
    return max(d) - min(d)


def _is_straight_pull(s: Stroke, line_h: float, min_len: float, strict: bool = False) -> bool:
    """A straight-ish pull of the pen (tremor, waves and a slope up to ~15 deg ok)."""
    f = stroke_features(s)
    (ax, ay), (bx, by) = s.points[0], s.points[-1]
    slope = abs(math.atan2(by - ay, (bx - ax) or 1e-6))
    slope = min(slope, math.pi - slope)
    # Pen waves keep going forward; handwriting loops back (x reversals).
    band = 0.9 if f.x_reversals == 0 and not strict else 0.6
    thin = _thickness(s.points) < max(band * line_h, 3.0)
    return (
        thin
        and f.x_reversals <= 2
        and slope < math.radians(15)
        and math.dist(s.points[0], s.points[-1]) > min_len
    )


def _is_cancel_stroke(s: Stroke, line_h: float) -> bool:
    return _is_straight_pull(s, line_h, 2.5 * line_h)


def _join_pulls(strokes: Sequence[Stroke], line_h: float) -> List[Stroke]:
    """Strikes drawn in several pulls along one line become one cancel stroke."""
    # Short pieces look like letter strokes, so only clearly thin pulls join.
    pulls = [s for s in strokes if _is_straight_pull(s, line_h, 0.8 * line_h, strict=True)]
    pulls.sort(key=lambda s: s.bbox[0])
    joined: List[Stroke] = []
    used: set = set()
    for i, a in enumerate(pulls):
        if id(a) in used:
            continue
        chain = [a]
        for b in pulls[i + 1 :]:
            if id(b) in used:
                continue
            last = chain[-1]
            ya = statistics.median(p[1] for p in last.points)
            yb = statistics.median(p[1] for p in b.points)
            gap = b.bbox[0] - last.bbox[2]  # the next pull starts where the last ended
            if abs(ya - yb) < 0.5 * line_h and -1.5 * line_h < gap < 1.2 * line_h:
                chain.append(b)
        if len(chain) > 1:
            pts = [p for st in chain for p in st.points]
            combo = Stroke(index=a.index, points=pts, tool=a.tool, color=a.color, width=a.width)
            if combo.bbox[2] - combo.bbox[0] > 2.5 * line_h:
                used.update(id(st) for st in chain)
                combo.parts = chain  # type: ignore[attr-defined]
                joined.append(combo)
    return joined


def _lines(strokes: Sequence[Stroke]) -> List[List[Stroke]]:
    """Group strokes into text lines by vertical overlap of their cores."""
    items = sorted(strokes, key=lambda s: (s.bbox[1] + s.bbox[3]) / 2)
    lines: List[List[Stroke]] = []
    spans: List[List[float]] = []
    for s in items:
        y0, y1 = s.bbox[1], s.bbox[3]
        # Use the middle 60% of the stroke so ascenders/descenders don't bridge lines.
        core0, core1 = y0 + 0.2 * (y1 - y0), y1 - 0.2 * (y1 - y0)
        if spans and core0 <= spans[-1][1]:
            lines[-1].append(s)
            spans[-1][1] = max(spans[-1][1], core1)
        else:
            lines.append([s])
            spans.append([core0, core1])
    return lines


def segment_entries(page: PageInk) -> List[Entry]:
    """Split one page's handwriting into entries."""
    strokes = [s for s in page.strokes if not s.is_highlighter]
    if not strokes:
        return []
    heights = [s.bbox[3] - s.bbox[1] for s in strokes if s.bbox[3] - s.bbox[1] > 1]
    line_h = statistics.median(heights) if heights else 8.0
    joined = _join_pulls(strokes, line_h)
    in_joined = {id(p) for j in joined for p in getattr(j, "parts", [])}
    cancels = joined + [
        s for s in strokes if id(s) not in in_joined and _is_cancel_stroke(s, line_h)
    ]
    writing = [s for s in strokes if s not in cancels and id(s) not in in_joined]
    pitch = LINE_PITCH if page.pdf_page is not None else max(2.2 * line_h, 18.0)
    groups = _group_lines(_lines(writing), pitch)
    if not groups:
        return []
    # A strike-shaped stroke that runs through no written line is ink of the
    # entry it sits by (a dash, an un-looped word, a rule under a heading).
    # It joins that entry after grouping: a divider drawn on the blank rule
    # between two requests must not bridge them into one.
    stray = [c for c in cancels if not any(_crosses_a_line(c, g) for g in groups)]
    stray_ids = {id(c) for c in stray}
    cancels = [c for c in cancels if id(c) not in stray_ids]
    extra: Dict[int, List[Stroke]] = {}
    for c in stray:
        k = _nearest_group(c, groups, pitch)
        if k is not None:
            extra.setdefault(k, []).extend(getattr(c, "parts", [c]))

    entries: List[Entry] = []
    for k, group in enumerate(groups):
        ink = [s for line in group for s in line] + extra.get(k, [])
        entry = Entry(page=page.page, strokes=ink)
        entry.fingerprints = {s.fingerprint() for s in entry.strokes}
        for c in cancels:
            if _crosses_a_line(c, group):
                entry.cancelled = True
                for part in getattr(c, "parts", [c]):
                    entry.fingerprints.add(part.fingerprint())
        entries.append(entry)
    return entries


def _nearest_group(
    stroke: Stroke, groups: Sequence[Sequence[Sequence[Stroke]]], pitch: float
) -> Optional[int]:
    """The entry whose ink is vertically closest (within a rule), if any."""
    y = statistics.median(p[1] for p in stroke.points)
    best, best_d = None, pitch
    for k, group in enumerate(groups):
        top = min(s.bbox[1] for line in group for s in line)
        bottom = max(s.bbox[3] for line in group for s in line)
        d = max(top - y, y - bottom, 0.0)
        if d < best_d:
            best, best_d = k, d
    return best


def _group_lines(lines: List[List[Stroke]], pitch: float) -> List[List[List[Stroke]]]:
    """Consecutive lines with no blank rule between them form one entry."""
    groups: List[List[List[Stroke]]] = []  # entries -> lines -> strokes
    last_bottom: Optional[float] = None
    for line in lines:
        top = min(s.bbox[1] for s in line)
        if groups and last_bottom is not None and top - last_bottom <= ENTRY_GAP * pitch:
            groups[-1].append(line)
        else:
            groups.append([line])
        last_bottom = max(s.bbox[3] for s in line)
    return groups


def _crosses_a_line(cancel: Stroke, lines: Sequence[Sequence[Stroke]]) -> bool:
    """A strike runs through the body of a written line; an underline does not.

    The body is where most of the line's ink is (25th-75th percentile of its
    points' heights) - not its bounding box, which ascender and descender
    loops stretch until the baseline sits inside it.
    """
    ys = sorted(p[1] for p in cancel.points)
    mid = ys[len(ys) // 2]
    cx0, _, cx1, _ = cancel.bbox
    for line in lines:
        lx0 = min(s.bbox[0] for s in line)
        lx1 = max(s.bbox[2] for s in line)
        if _overlap_1d(cx0, cx1, lx0, lx1) < 0.6 * (lx1 - lx0):
            continue  # must cross most of that line
        pts = sorted(p[1] for s in line for p in s.points)
        lo = pts[int(0.25 * (len(pts) - 1))]
        hi = pts[int(0.75 * (len(pts) - 1))]
        if lo <= mid <= hi:
            return True
    return False


def _overlap_1d(a0: float, a1: float, b0: float, b1: float) -> float:
    return max(0.0, min(a1, b1) - max(a0, b0))


def match_known(entry: Entry, known: Iterable[dict]) -> Optional[dict]:
    """The stored entry this one continues (>= half of its strokes reused)."""
    best, best_share = None, 0.0
    for k in known:
        old = set(k.get("fingerprints", []))
        if not old:
            continue
        share = len(old & entry.fingerprints) / len(old)
        if share > best_share:
            best, best_share = k, share
    return best if best_share >= 0.5 else None


def replies_markdown(replies: Sequence[dict]) -> str:
    stamp = datetime.now().strftime("%d %b %Y, %H:%M")
    out = [f"# Replies · {stamp}", ""]
    for r in replies:
        quote = (r.get("request") or "").strip().replace("\n", " ")
        out.append(f"## {quote[:80] or r['id']}")
        out.append("")
        out.append(r["reply"].strip())
        out.append("")
    return "\n".join(out)
