"""Review round-trip: send a Markdown draft, collect pen marks as change requests.

Pure functions over already-downloaded data; the MCP tools in
``workflows.tools`` handle transport, state and response shaping.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, replace
from typing import Dict, List, Optional, Sequence, Set, Tuple

from remarkable_mcp.workflows.ink.marks import Mark, TextBlock, analyze_page, default_blocks
from remarkable_mcp.workflows.ink.page import DocumentInk, PageInk
from remarkable_mcp.workflows.review.render import assign_words_to_blocks


@dataclass
class ChangeRequest:
    mark: Mark
    page: int
    block: Optional[dict]  # manifest block, when the document is a tracked review
    src_line: Optional[int]  # exact source line of the target text, when found
    new: bool

    def to_dict(self, note_text: Optional[str], note_status: str) -> dict:
        m = self.mark
        out: Dict[str, object] = {
            "id": m.id,
            "page": self.page,
            "kind": m.kind,
            "intent": m.intent,
            "target": m.target_text or None,
        }
        if self.block is not None:
            out["paragraph"] = int(self.block["id"][1:])
            out["src_lines"] = list(self.block["src_lines"])
            out["context"] = _excerpt(self.block["text"], m.target_text)
        elif m.block_ids:
            out["region"] = m.block_ids[0]
        if self.src_line:
            out["src_line"] = self.src_line
        if m.note is not None or m.kind == "note":
            out["note"] = note_text
            out["note_status"] = note_status
        if not self.new:
            out["seen_before"] = True
        return out


def _excerpt(text: str, target: str, width: int = 160) -> str:
    text = " ".join(text.split())
    if len(text) <= width:
        return text
    i = text.find(target[:40]) if target else -1
    if i < 0:
        return text[: width - 1] + "…"
    start = max(0, i - width // 3)
    end = min(len(text), start + width)
    return ("…" if start else "") + text[start:end] + ("…" if end < len(text) else "")


def body_view(page: PageInk, layout: Optional[dict]) -> PageInk:
    """The page with only body-text words (no margin numbers, header, footer).

    Review PDFs print paragraph numbers in the left margin and a header/footer;
    they are real text, and would otherwise become targets of margin bars.
    """
    if not layout or "text_x0" not in layout:
        return page
    x0 = layout["text_x0"] - 2
    top = layout.get("body_top", 0.0)
    bottom = layout.get("body_bottom", page.height)
    words = [w for w in page.words if w.rect[0] >= x0 and w.rect[1] >= top and w.rect[3] <= bottom]
    return replace(page, words=words)


def manifest_text_blocks(
    page: PageInk, manifest_blocks: Sequence[dict], layout: Optional[dict] = None
) -> List[TextBlock]:
    """Text blocks for ``page`` built from the review manifest.

    Words are assigned to blocks by block start position; each block's rect on
    this page is the union of its words. The PDF page number (not the tablet
    page) is used, so pages the reviewer inserted on the tablet don't shift
    anchoring.
    """
    if page.pdf_page is None:
        return []
    pdf_page = page.pdf_page + 1
    owners = assign_words_to_blocks(
        manifest_blocks,
        pdf_page,
        [w.rect for w in page.words],
        (layout or {}).get("body_pages"),
    )
    by_id: Dict[str, List[Tuple[float, float, float, float]]] = {}
    for word, owner in zip(page.words, owners):
        if owner:
            by_id.setdefault(owner, []).append(word.rect)
    blocks = []
    text_by_id = {b["id"]: b["text"] for b in manifest_blocks}
    for bid, rects in by_id.items():
        rect = (
            min(r[0] for r in rects),
            min(r[1] for r in rects),
            max(r[2] for r in rects),
            max(r[3] for r in rects),
        )
        blocks.append(TextBlock(id=bid, rect=rect, text=text_by_id.get(bid, "")))
    return blocks


def _norm(s: str) -> str:
    return re.sub(r"[\W_]+", " ", s).strip().lower()


def find_source_line(source: str, src_lines: Sequence[int], target: str) -> Optional[int]:
    """1-based line inside ``src_lines`` that contains (most of) ``target``."""
    if not source or not target or not src_lines or not src_lines[0]:
        return None
    lines = source.splitlines()
    lo, hi = max(1, src_lines[0]), min(len(lines), src_lines[1])
    want = _norm(target)
    if not want:
        return None
    words = want.split()
    best, best_score = None, 0.0
    for n in range(lo, hi + 1):
        line = _norm(lines[n - 1])
        if want in line:
            return n
        score = sum(1 for w in words if w in line) / len(words)
        if score > best_score:
            best, best_score = n, score
    return best if best_score >= 0.6 else None


def collect_requests(
    ink: DocumentInk,
    manifest_blocks: Optional[Sequence[dict]] = None,
    source_text: Optional[str] = None,
    seen: Optional[Set[str]] = None,
    layout: Optional[dict] = None,
) -> List[ChangeRequest]:
    """Analyse every annotated page into change requests, in reading order.

    ``seen`` holds stroke keys (Mark.seen_keys) returned before; a request is
    new while any of its strokes - including a note added later - is unseen.
    """
    seen = seen or set()
    by_id = {b["id"]: b for b in (manifest_blocks or [])}
    out: List[ChangeRequest] = []
    for page in ink.annotated_pages():
        if manifest_blocks:
            page = body_view(page, layout)
            blocks = manifest_text_blocks(page, manifest_blocks, layout)
        else:
            blocks = default_blocks(page)
        for mark in analyze_page(page, blocks):
            block = by_id.get(mark.block_ids[0]) if (mark.block_ids and by_id) else None
            src_line = None
            if block is not None and source_text and mark.target_text:
                src_line = find_source_line(source_text, block["src_lines"], mark.target_text)
            out.append(
                ChangeRequest(
                    mark=mark,
                    page=page.page,
                    block=block,
                    src_line=src_line,
                    new=not set(mark.seen_keys) <= seen,
                )
            )
    return out


def source_digest(text: str) -> str:
    return hashlib.sha1(text.encode()).hexdigest()[:16]
