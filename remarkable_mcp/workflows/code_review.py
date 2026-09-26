"""Code review on paper: a diff as a PDF, pen marks back as line comments.

The unified diff is laid out line by line in a monospace column (new-side
line numbers in the margin, removed lines in grey, a free right margin for
notes). Every rendered row is recorded with its file, side and line number,
so a mark anywhere on the page resolves to ``path:line`` - exactly what the
GitHub review API wants. A verdict row (Approve / Request changes / Comment)
at the end is read like a form.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence, Tuple

import pymupdf

from remarkable_mcp.workflows.forms import BOX, AnswerArea, _box, read_answers
from remarkable_mcp.workflows.ink import PageInk
from remarkable_mcp.workflows.marks import TextBlock, analyze_page
from remarkable_mcp.workflows.review_pdf import PAGE_H, PAGE_W

FONT = 6.6
LINE_H = 9.4
CODE_X = 58.0
NUM_X = 26.0
NOTE_MARGIN = 104.0
TOP = 40.0
BOTTOM = PAGE_H - 30.0
CHARS = int((PAGE_W - NOTE_MARGIN - CODE_X) / (0.6 * FONT))
VERDICTS = ["Approve", "Request changes", "Comment"]
_HUNK = re.compile(r"^@@ -(\d+)(?:,\d+)? \+(\d+)(?:,\d+)? @@(.*)$")


@dataclass
class DiffLine:
    kind: str  # "+", "-", " "
    text: str
    old: Optional[int]
    new: Optional[int]


@dataclass
class DiffFile:
    path: str
    hunks: List[Tuple[str, List[DiffLine]]] = field(default_factory=list)  # (header, lines)


def parse_diff(diff: str) -> List[DiffFile]:
    files: List[DiffFile] = []
    cur: Optional[DiffFile] = None
    old = new = 0
    for raw in diff.splitlines():
        if raw.startswith("diff --git "):
            cur = None
            continue
        if raw.startswith("+++ "):
            path = raw[4:].strip()
            path = path[2:] if path.startswith("b/") else path
            if path == "/dev/null":
                path = "(deleted file)"
            cur = DiffFile(path)
            files.append(cur)
            continue
        if raw.startswith("--- ") or cur is None:
            continue
        m = _HUNK.match(raw)
        if m:
            old, new = int(m.group(1)), int(m.group(2))
            cur.hunks.append((m.group(3).strip(), []))
            continue
        if not cur.hunks or raw.startswith("\\"):
            continue
        tag, text = (raw[:1] or " "), raw[1:]
        lines = cur.hunks[-1][1]
        if tag == "+":
            lines.append(DiffLine("+", text, None, new))
            new += 1
        elif tag == "-":
            lines.append(DiffLine("-", text, old, None))
            old += 1
        else:
            lines.append(DiffLine(" ", text, old, new))
            old += 1
            new += 1
    return [f for f in files if f.hunks]


@dataclass
class CodeRender:
    pdf: bytes
    rows: List[dict]  # {page, y0, y1, path, side, line, kind}
    verdict_areas: List[AnswerArea]
    page_count: int
    files: int
    changed_lines: int


def render_diff(title: str, files: Sequence[DiffFile], subtitle: str = "") -> CodeRender:
    doc = pymupdf.open()
    rows: List[dict] = []
    grey = (0.5, 0.5, 0.5)
    state = {"page": None, "y": BOTTOM + 1}

    def new_page():
        page = doc.new_page(width=PAGE_W, height=PAGE_H)
        head = f"{title} · {subtitle}" if subtitle else title
        page.insert_text((NUM_X, 24), head[:110], fontsize=6.5, fontname="helv", color=grey)
        page.draw_line(
            (PAGE_W - NOTE_MARGIN + 6, TOP),
            (PAGE_W - NOTE_MARGIN + 6, BOTTOM),
            color=(0.85, 0.85, 0.85),
            width=0.4,
        )
        state["page"], state["y"] = page, TOP

    def need(h: float):
        if state["y"] + h > BOTTOM:
            new_page()

    changed = 0
    for f in files:
        need(3 * LINE_H)
        state["y"] += 4
        state["page"].insert_text((NUM_X, state["y"] + 8), f.path[:80], fontsize=8, fontname="cobo")
        state["y"] += 13
        for header, lines in f.hunks:
            need(2 * LINE_H)
            if header:
                state["page"].insert_text(
                    (CODE_X, state["y"] + 7),
                    f"@@ {header}"[:CHARS],
                    fontsize=FONT,
                    fontname="cour",
                    color=grey,
                )
                state["y"] += LINE_H
            for dl in lines:
                chunks = [
                    dl.text[i : i + CHARS] for i in range(0, max(len(dl.text), 1), CHARS)
                ] or [""]
                for n, chunk in enumerate(chunks):
                    need(LINE_H)
                    page, y = state["page"], state["y"]
                    if dl.kind == "+":
                        page.draw_rect(
                            pymupdf.Rect(CODE_X - 3, y, PAGE_W - NOTE_MARGIN, y + LINE_H),
                            color=None,
                            fill=(0.93, 0.93, 0.93),
                        )
                        page.draw_line(
                            (CODE_X - 4, y), (CODE_X - 4, y + LINE_H), color=(0, 0, 0), width=1.2
                        )
                    if n == 0:
                        num = dl.new if dl.new is not None else dl.old
                        page.insert_text(
                            (NUM_X, y + 7), f"{num:>5}", fontsize=5.8, fontname="cour", color=grey
                        )
                        page.insert_text(
                            (CODE_X - 11, y + 7),
                            dl.kind,
                            fontsize=FONT,
                            fontname="cour",
                            color=grey if dl.kind == "-" else (0, 0, 0),
                        )
                    page.insert_text(
                        (CODE_X, y + 7),
                        chunk.expandtabs(4),
                        fontsize=FONT,
                        fontname="cour",
                        color=grey if dl.kind == "-" else (0, 0, 0),
                    )
                    rows.append(
                        {
                            "page": doc.page_count,
                            "y0": y,
                            "y1": y + LINE_H,
                            "path": f.path,
                            "side": "LEFT" if dl.kind == "-" else "RIGHT",
                            "line": dl.old if dl.kind == "-" else dl.new,
                            "kind": dl.kind,
                        }
                    )
                    state["y"] += LINE_H
                changed += dl.kind != " "
            state["y"] += 4

    # Verdict row.
    need(60)
    page = state["page"]
    y = state["y"] + 16
    page.insert_text((NUM_X, y), "Verdict", fontsize=10, fontname="hebo")
    y += 10
    areas: List[AnswerArea] = []
    x = NUM_X
    for v in VERDICTS:
        rect = _box(page, x, y)
        areas.append(AnswerArea("verdict", v, doc.page_count, rect))
        page.insert_text((x + BOX + 5, y + 9), v, fontsize=9, fontname="helv")
        x += BOX + 5 + pymupdf.get_text_length(v, fontname="helv", fontsize=9) + 22

    total = doc.page_count
    for i, pg in enumerate(doc, start=1):
        pg.insert_text(
            (PAGE_W - 40, PAGE_H - 14), f"{i}/{total}", fontsize=6.5, fontname="helv", color=grey
        )
    pdf = doc.tobytes(garbage=3, deflate=True)
    doc.close()
    return CodeRender(pdf, rows, areas, total, len(files), changed)


def _row_blocks(rows: Sequence[dict], pdf_page: int) -> List[TextBlock]:
    return [
        TextBlock(id=f"row{i}", rect=(CODE_X - 12, r["y0"], PAGE_W - NOTE_MARGIN, r["y1"]))
        for i, r in enumerate(rows)
        if r["page"] == pdf_page
    ]


@dataclass
class LineComment:
    path: str
    line: int
    side: str
    kind: str
    target: str
    mark_id: str
    note_strokes: list
    note_rect: Optional[Tuple[float, float, float, float]]
    seen_keys: List[str] = field(default_factory=list)


def collect_comments(pages: Dict[int, PageInk], rows: Sequence[dict]) -> List[LineComment]:
    """Marks on the diff pages -> comments on (path, line, side)."""
    out: List[LineComment] = []
    row_index = {f"row{i}": r for i, r in enumerate(rows)}
    for pno, page in sorted(pages.items()):
        blocks = _row_blocks(rows, pno)
        if not blocks:
            continue
        for mark in analyze_page(page, blocks):
            ids = [b for b in mark.block_ids if b in row_index]
            if not ids:
                continue
            # Comment on the last line the mark covers (GitHub anchors below it).
            row = row_index[ids[-1]]
            if row["line"] is None:
                continue
            note = mark if mark.kind == "note" else mark.note
            out.append(
                LineComment(
                    path=row["path"],
                    line=row["line"],
                    side=row["side"],
                    kind=mark.kind,
                    target=mark.target_text,
                    mark_id=mark.id,
                    note_strokes=list(note.strokes) if note else [],
                    note_rect=note.rect if note else None,
                    seen_keys=mark.seen_keys,
                )
            )
    return out


def comment_body(c: LineComment, note_text: Optional[str]) -> str:
    """A GitHub comment for a mark: the note if there is one, else what the mark says."""
    note = (note_text or "").strip()
    if c.kind in ("strikethrough", "scribble"):
        base = f"Remove `{c.target}`" if c.target else "Remove this"
        return f"{base} → {note}" if note else base + "."
    if note:
        return note
    if c.target:
        return f"Look at `{c.target}` (marked on paper, no note)."
    return "Marked on paper (see crop)."


def read_verdict(manifest_areas: Sequence[dict], pages: Dict[int, PageInk]) -> Optional[str]:
    fields = [{"id": "verdict", "type": "choice", "label": "Verdict", "options": VERDICTS}]
    [ans] = read_answers({"fields": fields, "areas": list(manifest_areas)}, pages)
    return ans.value


def github_event(verdict: Optional[str]) -> str:
    return {"Approve": "APPROVE", "Request changes": "REQUEST_CHANGES"}.get(
        verdict or "", "COMMENT"
    )
