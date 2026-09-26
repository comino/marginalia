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
_HUNK = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@(.*)$")


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


def _unquote_git_path(path: str) -> str:
    """Undo git's C-style path quoting ("b/\\303\\274.txt" -> "b/ü.txt")."""
    if not (len(path) >= 2 and path[0] == '"' and path[-1] == '"'):
        return path
    raw = path[1:-1]
    out = bytearray()
    i = 0
    simple = {
        "n": b"\n",
        "t": b"\t",
        '"': b'"',
        "\\": b"\\",
        "a": b"\a",
        "b": b"\b",
        "f": b"\f",
        "r": b"\r",
        "v": b"\v",
    }
    while i < len(raw):
        ch = raw[i]
        if ch == "\\" and i + 1 < len(raw):
            nxt = raw[i + 1]
            octal = raw[i + 1 : i + 4]
            if len(octal) == 3 and all(c in "01234567" for c in octal):
                out.append(int(octal, 8) & 0xFF)
                i += 4
                continue
            out += simple.get(nxt, nxt.encode())
            i += 2
            continue
        out += ch.encode()
        i += 1
    return out.decode("utf-8", "replace")


def _strip_prefix(path: str, prefix: str) -> str:
    path = _unquote_git_path(path.split("\t", 1)[0].strip())
    return path[2:] if path.startswith(prefix) else path


def parse_diff(diff: str) -> List[DiffFile]:
    """Parse a unified diff (git or plain) into files, hunks and numbered lines.

    Hunk bodies are consumed by the line counts announced in each ``@@``
    header, so content lines that happen to start with ``---``/``+++`` (a
    removed ``-- SQL comment``, an added ``++i``) are never mistaken for file
    headers. Only ``\\n`` splits lines: form feeds and other exotic line
    separators inside source lines stay inside them.
    """
    files: List[DiffFile] = []
    cur: Optional[DiffFile] = None
    old_path: Optional[str] = None
    old = new = 0
    old_left = new_left = 0  # lines still expected in the current hunk
    for raw in diff.split("\n"):
        if raw.endswith("\r"):
            raw = raw[:-1]
        if (old_left > 0 or new_left > 0) and raw.startswith("diff --git "):
            old_left = new_left = 0  # truncated hunk: the next file starts here
        if old_left > 0 or new_left > 0:
            if raw.startswith("\\"):
                continue  # "\ No newline at end of file"
            tag, text = (raw[:1] or " "), raw[1:]
            lines = cur.hunks[-1][1]
            if tag == "+":
                lines.append(DiffLine("+", text, None, new))
                new += 1
                new_left -= 1
            elif tag == "-":
                lines.append(DiffLine("-", text, old, None))
                old += 1
                old_left -= 1
            else:
                lines.append(DiffLine(" ", text, old, new))
                old += 1
                new += 1
                old_left -= 1
                new_left -= 1
            continue
        if raw.startswith("diff --git "):
            cur, old_path = None, None
            continue
        if raw.startswith("--- "):
            old_path = _strip_prefix(raw[4:], "a/")
            continue
        if raw.startswith("+++ "):
            path = _strip_prefix(raw[4:], "b/")
            if path == "/dev/null":  # deleted file: comment on its old path
                path = old_path or path
            cur = DiffFile(path)
            files.append(cur)
            continue
        m = _HUNK.match(raw)
        if m and cur is not None:
            old, new = int(m.group(1)), int(m.group(3))
            old_left = int(m.group(2)) if m.group(2) is not None else 1
            new_left = int(m.group(4)) if m.group(4) is not None else 1
            cur.hunks.append((m.group(5).strip(), []))
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
                text = dl.text.expandtabs(4)  # before wrapping, so wrapped rows fit the column
                chunks = [text[i : i + CHARS] for i in range(0, max(len(text), 1), CHARS)] or [""]
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
                        chunk,
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


def collect_comments(
    pages: Dict[int, PageInk],
    rows: Sequence[dict],
    exclude: Sequence[dict] = (),
) -> Tuple[List[LineComment], List[LineComment]]:
    """Marks on the diff pages -> (line comments, general remarks).

    A mark belongs to a line only if it sits on or right next to that row.
    Marks on the verdict boxes (``exclude``: manifest areas) are the verdict,
    not comments; handwriting away from any row (e.g. a summary under the last
    file) is a general remark for the review body, not a comment on whatever
    line happens to be nearest.
    """
    comments: List[LineComment] = []
    general: List[LineComment] = []
    row_index = {f"row{i}": r for i, r in enumerate(rows)}
    for pno, page in sorted(pages.items()):
        blocks = _row_blocks(rows, pno)
        keep_out = [
            (a["rect"][0] - 16, a["rect"][1] - 16, a["rect"][2] + 16, a["rect"][3] + 16)
            for a in exclude
            if a["page"] == pno
        ]
        for mark in analyze_page(page, blocks):
            if any(_overlaps(mark.rect, r) for r in keep_out):
                continue
            note = mark if mark.kind == "note" else mark.note
            item = LineComment(
                path="",
                line=0,
                side="RIGHT",
                kind=mark.kind,
                target=mark.target_text,
                mark_id=mark.id,
                note_strokes=list(note.strokes) if note else [],
                note_rect=note.rect if note else None,
                seen_keys=mark.seen_keys,
            )
            ids = [b for b in mark.block_ids if b in row_index]
            row = row_index[ids[-1]] if ids else None  # GitHub anchors below the last line
            near = row is not None and _vertical_gap(mark.rect, row) <= 1.5 * LINE_H
            if not near or row["line"] is None:
                general.append(item)
                continue
            item.path, item.line, item.side = row["path"], row["line"], row["side"]
            comments.append(item)
    return comments, general


def _overlaps(a, b) -> bool:
    return a[0] <= b[2] and b[0] <= a[2] and a[1] <= b[3] and b[1] <= a[3]


def _vertical_gap(rect, row: dict) -> float:
    if rect[3] < row["y0"]:
        return row["y0"] - rect[3]
    if rect[1] > row["y1"]:
        return rect[1] - row["y1"]
    return 0.0


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
