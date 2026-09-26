"""Load a document's pen strokes in PDF point space, next to the PDF's words.

Everything downstream (mark classification, anchoring, cropping) works in one
coordinate system: PDF points with a top-left origin on the page the ink sits
on. For PDF-backed documents that is the underlying PDF page; for notebooks it
is a virtual page the size of the device grid.

The stroke -> point transform is the same one the merged renderer uses
(``extract.render_merged_page_from_extracted_document``)::

    x_pt = rm_x * ppu + page_width_pt / 2      (rm x is centre-origin)
    y_pt = rm_y * ppu

with ``ppu`` the calibrated points-per-unit for the page's SceneInfo grid.
"""

from __future__ import annotations

import hashlib
import io
import json
import logging
import math
import tempfile
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, List, Optional, Tuple

from remarkable_mcp.extract import (
    _get_ordered_rm_files,
    _get_page_order,
    _points_per_unit,
    _resolve_pdf_page_index,
    _select_rm_file_for_page,
    _v6_blocks,
    _v6_paper_size,
)

logger = logging.getLogger(__name__)

# rmscene Pen values, grouped by what they mean for markup.
HIGHLIGHTER_TOOLS = {5, 18}
ERASER_TOOLS = {6, 8}
_TOOL_NAMES = {
    0: "paintbrush",
    1: "pencil",
    2: "ballpoint",
    3: "marker",
    4: "fineliner",
    5: "highlighter",
    7: "mechanical_pencil",
    12: "paintbrush",
    13: "mechanical_pencil",
    14: "pencil",
    15: "ballpoint",
    16: "marker",
    17: "fineliner",
    18: "highlighter",
    21: "calligraphy",
    23: "shader",
}
_COLOR_NAMES = {
    0: "black",
    1: "gray",
    2: "white",
    3: "yellow",
    4: "green",
    5: "pink",
    6: "blue",
    7: "red",
    8: "gray",
    9: "yellow",
    10: "green",
    11: "cyan",
    12: "magenta",
    13: "yellow",
}

Rect = Tuple[float, float, float, float]  # x0, y0, x1, y1 in PDF points


@dataclass
class Stroke:
    """One pen stroke on a page, in PDF points."""

    index: int  # drawing order on the page
    points: List[Tuple[float, float]]
    tool: str
    color: str
    width: float  # average nib width in points

    @property
    def bbox(self) -> Rect:
        xs = [p[0] for p in self.points]
        ys = [p[1] for p in self.points]
        return min(xs), min(ys), max(xs), max(ys)

    @property
    def is_highlighter(self) -> bool:
        return self.tool == "highlighter"

    @property
    def length(self) -> float:
        return sum(math.dist(a, b) for a, b in zip(self.points, self.points[1:]))

    def fingerprint(self) -> str:
        """Stable id for "have I seen this stroke before" (rounded geometry)."""
        h = hashlib.sha1()
        h.update(self.tool.encode())
        for x, y in self.points[:: max(1, len(self.points) // 16)]:
            h.update(f"{round(x)},{round(y)};".encode())
        return h.hexdigest()[:12]


@dataclass
class TextHighlight:
    """A native text highlight (GlyphRange) made with the highlighter on PDF text."""

    text: str
    rects: List[Rect]
    color: str


@dataclass
class Word:
    text: str
    rect: Rect
    block: int  # PyMuPDF block number
    line: int  # line number inside the block


@dataclass
class PageInk:
    page: int  # 1-based reMarkable page number
    pdf_page: Optional[int]  # 0-based page of the underlying PDF, None for notebook pages
    width: float
    height: float
    strokes: List[Stroke] = field(default_factory=list)
    highlights: List[TextHighlight] = field(default_factory=list)
    words: List[Word] = field(default_factory=list)

    @property
    def has_ink(self) -> bool:
        return bool(self.strokes or self.highlights)


@dataclass
class DocumentInk:
    pages: List[PageInk]
    pdf_bytes: Optional[bytes]
    page_count: int

    def annotated_pages(self) -> List[PageInk]:
        return [p for p in self.pages if p.has_ink]


def _enum_int(value, default: int = 0) -> int:
    value = getattr(value, "value", value)
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _page_strokes(
    blocks: list, page_w: float, ppu: float
) -> Tuple[List[Stroke], List[TextHighlight]]:
    """Convert v6 scene blocks into strokes/highlights in PDF points."""
    half_w = page_w / 2.0
    strokes: List[Stroke] = []
    highlights: List[TextHighlight] = []
    for block in blocks:
        item = getattr(getattr(block, "item", None), "value", None)
        if item is None:
            continue
        rectangles = getattr(item, "rectangles", None)
        if rectangles:
            rects = [
                (r.x * ppu + half_w, r.y * ppu, (r.x + r.w) * ppu + half_w, (r.y + r.h) * ppu)
                for r in rectangles
            ]
            highlights.append(
                TextHighlight(
                    text=(getattr(item, "text", "") or "").strip(),
                    rects=rects,
                    color=_COLOR_NAMES.get(_enum_int(getattr(item, "color", 9), 9), "yellow"),
                )
            )
            continue
        points = getattr(item, "points", None)
        if not points:
            continue
        tool = _enum_int(getattr(item, "tool", 0))
        if tool in ERASER_TOOLS:
            continue
        pts = [(p.x * ppu + half_w, p.y * ppu) for p in points]
        widths = [getattr(p, "width", 2) for p in points]
        strokes.append(
            Stroke(
                index=len(strokes),
                points=pts,
                tool=_TOOL_NAMES.get(tool, "pen"),
                color=_COLOR_NAMES.get(_enum_int(getattr(item, "color", 0)), "black"),
                width=(sum(widths) / len(widths)) * ppu / 4.0,
            )
        )
    return strokes, highlights


def _page_words(pdf_doc, pdf_page: int) -> List[Word]:
    words = []
    for x0, y0, x1, y1, text, block, line, _wn in pdf_doc[pdf_page].get_text("words"):
        words.append(Word(text=text, rect=(x0, y0, x1, y1), block=block, line=line))
    return words


def load_document_ink(extracted: Path, pages: Optional[Iterable[int]] = None) -> DocumentInk:
    """Load ink for an extracted reMarkable document directory.

    ``pages`` restricts loading to those 1-based page numbers.
    """
    import fitz

    pdf_files = list(extracted.glob("**/*.pdf"))
    content_stems = {p.stem for p in extracted.glob("*.content")}
    matching = [p for p in pdf_files if p.stem in content_stems]
    pdf_path = matching[0] if matching else (sorted(pdf_files)[0] if pdf_files else None)
    pdf_bytes = pdf_path.read_bytes() if pdf_path else None
    pdf_doc = fitz.open(stream=pdf_bytes, filetype="pdf") if pdf_bytes else None

    rm_files = _get_ordered_rm_files(extracted)
    page_count = len(_get_page_order(extracted)) or len(rm_files)
    if page_count == 0 and pdf_doc is not None:
        page_count = len(pdf_doc)

    wanted = set(pages) if pages is not None else None
    result: List[PageInk] = []
    try:
        for page in range(1, page_count + 1):
            if wanted is not None and page not in wanted:
                continue
            rm_file = _select_rm_file_for_page(extracted, rm_files, page)
            blocks = _v6_blocks(rm_file) if rm_file is not None else None
            paper = _v6_paper_size(blocks) if blocks else None
            ppu = _points_per_unit(paper)

            pdf_page = _resolve_pdf_page_index(extracted, page) if pdf_doc is not None else None
            if pdf_page is not None and pdf_page < len(pdf_doc):
                rect = pdf_doc[pdf_page].rect
                width, height = rect.width, rect.height
                words = _page_words(pdf_doc, pdf_page)
            else:
                pdf_page = None
                grid_w, grid_h = paper or (1404.0, 1872.0)
                width, height = grid_w * ppu, grid_h * ppu
                words = []

            strokes, highlights = _page_strokes(blocks, width, ppu) if blocks else ([], [])
            result.append(
                PageInk(
                    page=page,
                    pdf_page=pdf_page,
                    width=width,
                    height=height,
                    strokes=strokes,
                    highlights=highlights,
                    words=words,
                )
            )
    finally:
        if pdf_doc is not None:
            pdf_doc.close()
    return DocumentInk(pages=result, pdf_bytes=pdf_bytes, page_count=page_count)


def load_document_ink_from_zip(
    zip_bytes: bytes, pages: Optional[Iterable[int]] = None
) -> DocumentInk:
    """Load ink from the zip payload a transport's ``download()`` returns."""
    with tempfile.TemporaryDirectory() as tmp:
        with zipfile.ZipFile(io.BytesIO(zip_bytes)) as zf:
            zf.extractall(tmp)
        return load_document_ink(Path(tmp), pages)


def document_modified_token(zip_bytes: bytes) -> str:
    """Hash of every .rm page in a document zip: changes iff the ink changed."""
    h = hashlib.sha1()
    with zipfile.ZipFile(io.BytesIO(zip_bytes)) as zf:
        for name in sorted(n for n in zf.namelist() if n.endswith(".rm")):
            h.update(name.encode())
            h.update(zf.read(name))
    return h.hexdigest()[:16]


def content_json(zip_bytes: bytes) -> dict:
    """Return the parsed .content file from a document zip (or {})."""
    with zipfile.ZipFile(io.BytesIO(zip_bytes)) as zf:
        for name in zf.namelist():
            if name.endswith(".content"):
                try:
                    return json.loads(zf.read(name))
                except ValueError:
                    return {}
    return {}
