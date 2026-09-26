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

from remarkable_mcp.workflows.ink.marks import Mark, analyze_page
from remarkable_mcp.workflows.ink.page import PageInk


class SyncTexError(RuntimeError):
    pass


def synctex_available() -> bool:
    return shutil.which("synctex") is not None


def synctex_inputs(sync: Path) -> List[str]:
    """Absolute paths of the input files recorded in a SyncTeX file."""
    import gzip

    opener = gzip.open if sync.suffix == ".gz" else open
    out: List[str] = []
    with opener(sync, "rt", errors="replace") as fh:
        for line in fh:
            if line.startswith("Input:"):
                path = line.split(":", 2)[-1].strip()
                if path:
                    out.append(str(Path(path).resolve()))
    return out


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


_WORD = re.compile(r"\w+", re.UNICODE)


def _refine(text: str, line: int, target: str, reach: int = 3) -> int:
    """Move ``line`` to the nearest line (searching outward) that contains the
    marked words as whole words; SyncTeX's answer wins ties and misses."""
    lines = text.splitlines()
    words = [w.lower() for w in _WORD.findall(target or "")]
    if not words or not (0 < line <= len(lines)):
        return line

    def score(n: int) -> float:
        have = {w.lower() for w in _WORD.findall(lines[n - 1])}
        return sum(1 for w in words if w in have) / len(words)

    if score(line) >= 0.6:
        return line
    for d in range(1, reach + 1):
        for n in (line - d, line + d):
            if 0 < n <= len(lines) and score(n) >= 0.6:
                return n
    return line


def page_offsets(pdf: Path) -> Dict[int, Tuple[float, float]]:
    """Per page: (dx, dy) from PyMuPDF page coordinates to SyncTeX coordinates.

    PyMuPDF measures from the CropBox's top-left corner, SyncTeX from the
    MediaBox's; for most PDFs both are the same and the offset is (0, 0).
    """
    import fitz

    with fitz.open(pdf) as doc:
        return {n: (p.cropbox_position.x, p.cropbox_position.y) for n, p in enumerate(doc, 1)}


def collect_tex_requests(
    pdf: Path, pages: Dict[int, PageInk], sources: Optional[Dict[str, str]] = None
) -> List[TexRequest]:
    """Marks on each page (keyed by 1-based PDF page) resolved to .tex locations.

    ``sources`` maps absolute input paths to the text they had at compile time
    (from the snapshot); without it the current files are read.
    """
    out: List[TexRequest] = []
    offsets = page_offsets(pdf)
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
            dx, dy = offsets.get(pno, (0.0, 0.0))
            loc = synctex_edit(pdf, pno, x + dx, y + dy)
            file = line = src = None
            if loc:
                file, line = loc
                text = (sources or {}).get(file)
                if text is None:
                    try:
                        text = Path(file).read_text(errors="replace")
                    except OSError:
                        text = ""
                line = _refine(text, line, mark.target_text)
                lines = text.splitlines()
                src = lines[line - 1] if 0 < line <= len(lines) else None
            out.append(TexRequest(mark, pno, file, line, src))
    return out
