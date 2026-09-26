"""Review compiled LaTeX on the tablet; map pen marks to .tex file and line via SyncTeX.

The PDF produced by ``pdflatex/lualatex/latexmk -synctex=1`` is sent as is
(page layout unchanged). Marks are classified against the PDF's own text like
any annotated document, then each mark's anchor point is resolved with
``synctex edit`` to the exact input file and line - including files pulled in
with \\input / \\include. The marked words are then searched in a few lines
around that position to pin the line down further.
"""

from __future__ import annotations

import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from remarkable_mcp.workflows.ink import PageInk
from remarkable_mcp.workflows.marks import Mark, analyze_page
from remarkable_mcp.workflows.review import find_source_line


class SyncTexError(RuntimeError):
    pass


def synctex_available() -> bool:
    return shutil.which("synctex") is not None


def synctex_file(pdf: Path) -> Optional[Path]:
    for suffix in (".synctex.gz", ".synctex"):
        cand = pdf.with_suffix(suffix)
        if cand.exists():
            return cand
    return None


_FIELD = re.compile(r"^(Input|Line|Column):(.*)$")


def synctex_edit(pdf: Path, page: int, x: float, y: float) -> Optional[Tuple[str, int]]:
    """(input file, line) for a point in PDF points (top-left origin), or None."""
    res = subprocess.run(
        ["synctex", "edit", "-o", f"{page}:{x:.1f}:{y:.1f}:{pdf}"],
        capture_output=True,
        text=True,
        timeout=20,
    )
    if res.returncode != 0:
        raise SyncTexError(res.stderr.strip() or "synctex failed")
    found: Dict[str, str] = {}
    for line in res.stdout.splitlines():
        m = _FIELD.match(line.strip())
        if m and m.group(1) not in found:
            found[m.group(1)] = m.group(2).strip()
    if "Input" not in found or "Line" not in found:
        return None
    path = str(Path(found["Input"]).resolve())
    try:
        return path, int(found["Line"])
    except ValueError:
        return None


@dataclass
class TexRequest:
    mark: Mark
    page: int
    file: Optional[str]
    line: Optional[int]
    source: Optional[str]  # the .tex line text


def _anchor(mark: Mark) -> Tuple[float, float]:
    """Point to resolve: the middle of the first marked word, else the mark's centre."""
    if mark.words:
        w = sorted(mark.words, key=lambda w: (w.rect[1], w.rect[0]))[0]
        return (w.rect[0] + w.rect[2]) / 2, (w.rect[1] + w.rect[3]) / 2
    r = mark.rect
    return (r[0] + r[2]) / 2, (r[1] + r[3]) / 2


def _refine(file: str, line: int, target: str) -> Tuple[int, Optional[str]]:
    try:
        text = Path(file).read_text(errors="replace")
    except OSError:
        return line, None
    lines = text.splitlines()
    better = find_source_line(text, (max(1, line - 3), min(len(lines), line + 3)), target)
    line = better or line
    src = lines[line - 1] if 0 < line <= len(lines) else None
    return line, src


def collect_tex_requests(pdf: Path, pages: Dict[int, PageInk]) -> List[TexRequest]:
    """Marks on each page (keyed by 1-based PDF page) resolved to .tex locations."""
    out: List[TexRequest] = []
    for pno, page in sorted(pages.items()):
        if not page.has_ink:
            continue
        for mark in analyze_page(page):
            # A margin note is about the text beside it: resolve at the word on
            # that line nearest to the note (line starts often map to the
            # enclosing paragraph's first source line, not this sentence).
            if mark.kind == "note" and not mark.words and page.words:
                cy = (mark.rect[1] + mark.rect[3]) / 2
                row = min(page.words, key=lambda w: abs((w.rect[1] + w.rect[3]) / 2 - cy))
                rcy = (row.rect[1] + row.rect[3]) / 2
                same_line = [
                    w
                    for w in page.words
                    if abs((w.rect[1] + w.rect[3]) / 2 - rcy) < 0.4 * (w.rect[3] - w.rect[1])
                ]
                beside = min(
                    same_line,
                    key=lambda w: max(0.0, mark.rect[0] - w.rect[2], w.rect[0] - mark.rect[2]),
                )
                x, y = (beside.rect[0] + beside.rect[2]) / 2, rcy
            else:
                x, y = _anchor(mark)
            loc = synctex_edit(pdf, pno, x, y)
            file = line = src = None
            if loc:
                file, line = loc
                line, src = _refine(file, line, mark.target_text)
            out.append(TexRequest(mark, pno, file, line, src))
    return out
