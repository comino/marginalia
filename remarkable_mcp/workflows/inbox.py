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
import statistics
from dataclasses import dataclass, field
from datetime import datetime
from typing import Iterable, List, Optional, Sequence, Set

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


def _is_cancel_stroke(s: Stroke, line_h: float) -> bool:
    """A long, flat, straight-ish stroke (waves and tremor allowed)."""
    f = stroke_features(s)
    flat = f.h < max(0.6 * line_h, 4.0) and f.x_reversals <= 2
    return (
        (f.smooth_straightness > 0.85 or flat)
        and f.sagitta < 0.6 * line_h
        and f.w > 2.5 * line_h
        and f.h < 1.5 * line_h
    )


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
    cancels = [s for s in strokes if _is_cancel_stroke(s, line_h)]
    writing = [s for s in strokes if s not in cancels]
    lines = _lines(writing)
    if not lines:
        return []

    pitch = LINE_PITCH if page.pdf_page is not None else max(2.2 * line_h, 18.0)
    groups: List[List[List[Stroke]]] = []  # entries -> lines -> strokes
    last_bottom: Optional[float] = None
    for line in lines:
        top = min(s.bbox[1] for s in line)
        if groups and last_bottom is not None and top - last_bottom <= ENTRY_GAP * pitch:
            groups[-1].append(line)
        else:
            groups.append([line])
        last_bottom = max(s.bbox[3] for s in line)

    entries: List[Entry] = []
    for group in groups:
        entry = Entry(page=page.page, strokes=[s for line in group for s in line])
        entry.fingerprints = {s.fingerprint() for s in entry.strokes}
        for c in cancels:
            if _crosses_a_line(c, group, entry.rect):
                entry.cancelled = True
                entry.fingerprints.add(c.fingerprint())
        entries.append(entry)
    return entries


def _crosses_a_line(cancel: Stroke, lines: Sequence[Sequence[Stroke]], rect: Rect) -> bool:
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
