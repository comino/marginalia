"""Paper forms: render fields with known geometry, read answers from ink.

Every answer area is a rectangle recorded in the form manifest, so reading a
form needs no recognition for choices: an option counts as selected when there
is enough ink inside its box (a tick or cross) or a loop around it (a circle).
Only write-in fields go through handwriting transcription.

Field types::

    checkbox  {"id", "type": "checkbox", "label"}                    -> bool
    choice    {"id", "type": "choice", "label", "options": [...]}    -> str | None
    multi     {"id", "type": "multi", "label", "options": [...]}     -> [str]
    scale     {"id", "type": "scale", "label", "min": 1, "max": 5,
               "min_label"?, "max_label"?}                           -> int | None
    text      {"id", "type": "text", "label", "lines": 2}            -> handwriting
    heading   {"type": "heading", "label"}                           (layout only)
    info      {"type": "info", "label"}                              (layout only)
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime
from typing import Dict, List, Optional, Sequence, Tuple

import pymupdf

from remarkable_mcp.workflows.ink.marks import stroke_features
from remarkable_mcp.workflows.ink.page import PageInk, Rect, Stroke
from remarkable_mcp.workflows.review.render import PAGE_H, PAGE_W

FIELD_TYPES = {"checkbox", "choice", "multi", "scale", "text", "heading", "info", "image"}
_ANSWER_TYPES = FIELD_TYPES - {"heading", "info", "image"}

MARGIN_X = 40.0
TOP = 44.0
BOTTOM = PAGE_H - 36.0
BOX = 11.0
LINE_GAP = 26.0  # ruled line spacing for write-in fields
_BLACK = (0, 0, 0)
_GREY = (0.45, 0.45, 0.45)
_RULE = (0.7, 0.7, 0.7)


class FormSpecError(ValueError):
    pass


@dataclass
class AnswerArea:
    field_id: str
    option: Optional[str]  # option label; None for checkbox/text
    page: int  # 1-based
    rect: Rect
    label_rect: Optional[Rect] = None  # the printed label (circling it selects too)


@dataclass
class FormRender:
    pdf: bytes
    areas: List[AnswerArea]
    page_count: int
    fields: List[dict]

    def manifest(self) -> dict:
        return {
            # Embedded image bytes stay out of the stored manifest.
            "fields": [{k: v for k, v in f.items() if k != "png"} for f in self.fields],
            "areas": [
                {
                    "field": a.field_id,
                    "option": a.option,
                    "page": a.page,
                    "rect": list(a.rect),
                    **({"label_rect": list(a.label_rect)} if a.label_rect else {}),
                }
                for a in self.areas
            ],
        }


def validate_fields(fields: Sequence[dict]) -> List[dict]:
    """Normalise and check a field list; raises FormSpecError with a clear message."""
    if not fields:
        raise FormSpecError("A form needs at least one field.")
    out: List[dict] = []
    seen = set()
    for n, raw in enumerate(fields, start=1):
        if not isinstance(raw, dict):
            raise FormSpecError(f"Field {n} must be an object.")
        f = dict(raw)
        ftype = f.get("type", "checkbox")
        if ftype not in FIELD_TYPES:
            raise FormSpecError(
                f"Field {n}: unknown type '{ftype}'. Use one of {sorted(FIELD_TYPES)}."
            )
        f["type"] = ftype
        if not str(f.get("label", "")).strip():
            raise FormSpecError(f"Field {n}: 'label' is required.")
        if ftype in _ANSWER_TYPES:
            fid = str(f.get("id") or f"q{n}")
            if fid in seen:
                raise FormSpecError(f"Duplicate field id '{fid}'.")
            seen.add(fid)
            f["id"] = fid
        if ftype == "image":
            # Image bytes can only come from the server itself (e.g. a crop of
            # the user's mark); callers pass a path, which is vetted.
            if not isinstance(f.get("png"), (bytes, bytearray)):
                f.pop("png", None)
                from remarkable_mcp.workflows.safety import UnsafeInput, check_local_file

                try:
                    path = check_local_file(
                        str(f.get("path") or ""), (".png", ".jpg", ".jpeg"), 10_000_000, "image"
                    )
                except UnsafeInput as exc:
                    raise FormSpecError(f"Field {n}: {exc}") from exc
                f["png"] = path.read_bytes()
        if ftype in ("choice", "multi"):
            opts = [str(o) for o in f.get("options") or []]
            if len(opts) < 2:
                raise FormSpecError(f"Field '{f.get('id')}': choice fields need 2+ options.")
            f["options"] = opts
        if ftype == "scale":
            lo, hi = int(f.get("min", 1)), int(f.get("max", 5))
            if not 0 <= lo < hi or hi - lo > 10:
                raise FormSpecError(
                    f"Field '{f['id']}': scale needs 0 <= min < max, at most 11 steps."
                )
            f["min"], f["max"] = lo, hi
        if ftype == "text":
            f["lines"] = max(1, min(12, int(f.get("lines", 2))))
        out.append(f)
    if not any(f["type"] in _ANSWER_TYPES for f in out):
        raise FormSpecError("A form needs at least one answerable field.")
    return out


class _Layout:
    """Top-down flow layout with page breaks."""

    def __init__(self, doc, title: str, subtitle: str):
        self.doc = doc
        self.title = title
        self.subtitle = subtitle
        self.page = None
        self.y = 0.0
        self.width = PAGE_W - 2 * MARGIN_X
        self.new_page()

    @property
    def page_no(self) -> int:
        return len(self.doc)

    def new_page(self) -> None:
        self.page = self.doc.new_page(width=PAGE_W, height=PAGE_H)
        header = self.title + (f" · {self.subtitle}" if self.subtitle else "")
        self.page.insert_text(
            (MARGIN_X, 24), header[:100], fontsize=6.5, fontname="helv", color=_GREY
        )
        self.y = TOP

    def ensure(self, height: float) -> None:
        if self.y + height > BOTTOM:
            self.new_page()

    def measure(self, text: str, size: float, indent: float = 0.0, bold: bool = False) -> float:
        """Height ``text`` would take at the cursor (for keeping boxes with labels)."""
        font = "hebo" if bold else "helv"
        return len(_wrap(text, size, self.width - indent, font)) * size * 1.35

    def text(
        self, text: str, size: float, bold: bool = False, color=_BLACK, indent: float = 0.0
    ) -> float:
        """Write wrapped text at the cursor; returns its height."""
        font = "hebo" if bold else "helv"
        width = self.width - indent
        lines = _wrap(text, size, width, font)
        height = len(lines) * size * 1.35
        self.ensure(height)
        for i, line in enumerate(lines):
            self.page.insert_text(
                (MARGIN_X + indent, self.y + size + i * size * 1.35),
                line,
                fontsize=size,
                fontname=font,
                color=color,
            )
        self.y += height
        return height


def _wrap(text: str, size: float, width: float, font: str) -> List[str]:
    lines: List[str] = []
    for para in str(text).split("\n"):
        words, cur = para.split(), ""
        for w in words:
            trial = f"{cur} {w}".strip()
            if pymupdf.get_text_length(trial, fontname=font, fontsize=size) <= width or not cur:
                cur = trial
            else:
                lines.append(cur)
                cur = w
        lines.append(cur)
    return lines or [""]


def _place_image(lay: "_Layout", png: bytes, caption: str, max_h: float = 190.0) -> None:
    """Embed a PNG scaled into the column (e.g. a crop of the user's own mark)."""
    pix = pymupdf.Pixmap(png)
    w, h = pix.width, pix.height
    scale = min(lay.width / w, max_h / h, 1.5)
    dw, dh = w * scale, h * scale
    lay.ensure(dh + 24)
    rect = pymupdf.Rect(MARGIN_X, lay.y, MARGIN_X + dw, lay.y + dh)
    lay.page.insert_image(rect, stream=png)
    lay.page.draw_rect(rect, color=(0.6, 0.6, 0.6), width=0.5)
    lay.y += dh + 4
    if caption:
        lay.text(caption, 7.5, color=_GREY)
    lay.y += 10


def _label_rect(text: str, size: float, x: float, y: float, h: float, width: float) -> Rect:
    first = _wrap(text, size, width, "helv")[0]
    return (x, y - 1, x + pymupdf.get_text_length(first, fontname="helv", fontsize=size), y - 1 + h)


def _box(page, x: float, y: float, size: float = BOX) -> Rect:
    rect = (x, y, x + size, y + size)
    page.draw_rect(pymupdf.Rect(*rect), color=_BLACK, width=0.8)
    return rect


def render_form(
    title: str,
    fields: Sequence[dict],
    intro: str = "",
    subtitle: str = "",
) -> FormRender:
    fields = validate_fields(fields)
    doc = pymupdf.open()
    subtitle = subtitle or datetime.now().strftime("%d %b %Y")
    lay = _Layout(doc, title, subtitle)
    areas: List[AnswerArea] = []

    lay.text(title, 16, bold=True)
    lay.y += 4
    if intro:
        lay.text(intro, 9.5, color=(0.2, 0.2, 0.2))
    lay.y += 10

    number = 0
    for f in fields:
        t = f["type"]
        if t == "heading":
            lay.ensure(40)
            lay.y += 8
            lay.text(f["label"], 12, bold=True)
            lay.y += 6
            continue
        if t == "info":
            lay.text(f["label"], 9, color=(0.25, 0.25, 0.25))
            lay.y += 8
            continue
        if t == "image":
            _place_image(lay, f["png"], f["label"], max_h=float(f.get("max_height", 190)))
            continue
        if t == "checkbox":
            lay.ensure(max(lay.measure(f["label"], 10.5, BOX + 8), BOX) + 10)
            box = _box(lay.page, MARGIN_X, lay.y)
            saved = lay.y
            lay.y -= 1
            h = lay.text(f"{f['label']}", 10.5, indent=BOX + 8)
            label = _label_rect(f["label"], 10.5, MARGIN_X + BOX + 8, saved, h, lay.width - BOX - 8)
            areas.append(AnswerArea(f["id"], None, lay.page_no, box, label))
            lay.y = saved + max(h, BOX) + 12
            continue

        number += 1
        lay.ensure(40)
        lay.text(f"{number}. {f['label']}", 10.5, bold=True)
        lay.y += 6
        if t in ("choice", "multi"):
            if t == "multi":
                lay.text("Tick all that apply.", 7.5, color=_GREY, indent=8)
                lay.y += 3
            for opt in f["options"]:
                lay.ensure(max(lay.measure(opt, 10, 8 + BOX + 8), BOX) + 8)
                rect = _box(lay.page, MARGIN_X + 8, lay.y, BOX)
                saved = lay.y
                lay.y -= 1
                h = lay.text(opt, 10, indent=8 + BOX + 8)
                label = _label_rect(opt, 10, MARGIN_X + 8 + BOX + 8, saved, h, lay.width - BOX - 16)
                areas.append(AnswerArea(f["id"], opt, lay.page_no, rect, label))
                lay.y = saved + max(h, BOX) + 9
        elif t == "scale":
            steps = list(range(f["min"], f["max"] + 1))
            size = 20.0
            gap = min(14.0, (lay.width - 16 - len(steps) * size) / max(1, len(steps) - 1))
            lay.ensure(size + 24)
            x = MARGIN_X + 8
            for v in steps:
                rect = (x, lay.y, x + size, lay.y + size)
                lay.page.draw_rect(pymupdf.Rect(*rect), color=_BLACK, width=0.8)
                label = str(v)
                tw = pymupdf.get_text_length(label, fontname="helv", fontsize=10)
                lay.page.insert_text(
                    (x + (size - tw) / 2, lay.y + 14), label, fontsize=10, fontname="helv"
                )
                areas.append(AnswerArea(f["id"], label, lay.page_no, rect))
                x += size + gap
            lay.y += size + 3
            ends = [f.get("min_label", ""), f.get("max_label", "")]
            if any(ends):
                lay.page.insert_text(
                    (MARGIN_X + 8, lay.y + 8), ends[0], fontsize=7, fontname="helv", color=_GREY
                )
                tw = pymupdf.get_text_length(ends[1], fontname="helv", fontsize=7)
                lay.page.insert_text(
                    (x - gap - tw, lay.y + 8), ends[1], fontsize=7, fontname="helv", color=_GREY
                )
                lay.y += 10
        elif t == "text":
            height = f["lines"] * LINE_GAP
            lay.ensure(height + 6)
            top = lay.y
            for i in range(1, f["lines"] + 1):
                yy = top + i * LINE_GAP
                lay.page.draw_line(
                    (MARGIN_X + 8, yy), (PAGE_W - MARGIN_X, yy), color=_RULE, width=0.5
                )
            areas.append(
                AnswerArea(
                    f["id"],
                    None,
                    lay.page_no,
                    (MARGIN_X + 4, top, PAGE_W - MARGIN_X, top + height + 6),
                )
            )
            lay.y = top + height + 4
        lay.y += 14

    total = len(doc)
    for i, page in enumerate(doc, start=1):
        page.insert_text(
            (PAGE_W - 40, PAGE_H - 14), f"{i}/{total}", fontsize=6.5, fontname="helv", color=_GREY
        )
    pdf = doc.tobytes(garbage=3, deflate=True)
    doc.close()
    return FormRender(pdf=pdf, areas=areas, page_count=total, fields=fields)


def render_triage(
    title: str,
    items: Sequence[dict],
    options: Sequence[str],
    intro: str = "",
) -> FormRender:
    """Compact rows: one item per row, the same option boxes in columns on the right.

    ``items``: [{"id", "title", "subtitle"?}]. Each row becomes a ``choice``
    field, so reading uses the normal form machinery.
    """
    options = [str(o) for o in options]
    if len(options) < 2 or len(options) > 5:
        raise FormSpecError("Triage needs 2-5 options.")
    if not items:
        raise FormSpecError("Triage needs at least one item.")
    ids = [str(it.get("id") or f"r{n}") for n, it in enumerate(items, start=1)]
    if len(set(ids)) != len(ids):
        raise FormSpecError("Item ids must be unique.")

    col_w = 44.0
    cols_x0 = PAGE_W - MARGIN_X - col_w * len(options)
    text_w = cols_x0 - MARGIN_X - 8
    doc = pymupdf.open()
    lay = _Layout(doc, title, datetime.now().strftime("%d %b %Y"))
    lay.text(title, 15, bold=True)
    if intro:
        lay.y += 2
        lay.text(intro, 9, color=(0.2, 0.2, 0.2))
    lay.y += 8

    def header():
        for k, opt in enumerate(options):
            label = opt[:10]
            tw = pymupdf.get_text_length(label, fontname="hebo", fontsize=7)
            cx = cols_x0 + col_w * k + col_w / 2
            lay.page.insert_text((cx - tw / 2, lay.y + 8), label, fontsize=7, fontname="hebo")
        lay.y += 14

    header()
    fields: List[dict] = []
    areas: List[AnswerArea] = []
    for item_id, it in zip(ids, items):
        head = _wrap(str(it.get("title", item_id)), 9.5, text_w, "hebo")[:2]
        sub = (
            _wrap(str(it.get("subtitle", "")), 7.5, text_w, "helv")[:2]
            if it.get("subtitle")
            else []
        )
        row_h = max(len(head) * 12 + len(sub) * 9.5 + 10, BOX + 14)
        if lay.y + row_h > BOTTOM:
            lay.new_page()
            header()
        top = lay.y
        yy = top + 10
        for line in head:
            lay.page.insert_text((MARGIN_X, yy), line, fontsize=9.5, fontname="hebo")
            yy += 12
        for line in sub:
            lay.page.insert_text(
                (MARGIN_X, yy - 2), line, fontsize=7.5, fontname="helv", color=_GREY
            )
            yy += 9.5
        by = top + (row_h - BOX) / 2 - 2
        for k, opt in enumerate(options):
            bx = cols_x0 + col_w * k + (col_w - BOX) / 2
            areas.append(AnswerArea(item_id, opt, lay.page_no, _box(lay.page, bx, by)))
        lay.y = top + row_h
        lay.page.draw_line(
            (MARGIN_X, lay.y - 2), (PAGE_W - MARGIN_X, lay.y - 2), color=_RULE, width=0.4
        )
        fields.append(
            {
                "id": item_id,
                "type": "choice",
                "label": str(it.get("title", item_id)),
                "options": options,
            }
        )

    total = len(doc)
    for i, page in enumerate(doc, start=1):
        page.insert_text(
            (PAGE_W - 40, PAGE_H - 14), f"{i}/{total}", fontsize=6.5, fontname="helv", color=_GREY
        )
    pdf = doc.tobytes(garbage=3, deflate=True)
    doc.close()
    return FormRender(pdf=pdf, areas=areas, page_count=total, fields=fields)


def nearest_field(manifest: dict, page: int, rect: Rect) -> Optional[str]:
    """The field whose answer areas sit closest (vertically) to ``rect`` on ``page``."""
    cy = (rect[1] + rect[3]) / 2
    best, dist = None, math.inf
    for a in manifest["areas"]:
        if a["page"] != page:
            continue
        r = a["rect"]
        d = 0.0 if r[1] <= cy <= r[3] else min(abs(cy - r[1]), abs(cy - r[3]))
        if d < dist:
            best, dist = a["field"], d
    return best if dist <= 30 else None


# --------------------------------------------------------------------------- reading


def _inside(p: Tuple[float, float], r: Rect) -> bool:
    return r[0] <= p[0] <= r[2] and r[1] <= p[1] <= r[3]


def ink_length_in(rect: Rect, strokes: Sequence[Stroke]) -> float:
    """Length of pen path inside ``rect`` (segment midpoints tested)."""
    total = 0.0
    for s in strokes:
        if s.is_highlighter:
            continue
        for a, b in zip(s.points, s.points[1:]):
            if _inside(((a[0] + b[0]) / 2, (a[1] + b[1]) / 2), rect):
                total += math.dist(a, b)
    return total


def _encloses(stroke: Stroke, rect: Rect) -> bool:
    """A loop drawn around the box (circling an option)."""
    from remarkable_mcp.workflows.ink.marks import _point_in_polygon

    f = stroke_features(stroke)
    sx0, sy0, sx1, sy1 = stroke.bbox
    w, h = rect[2] - rect[0], rect[3] - rect[1]
    if f.inner > 0.15 or (sx1 - sx0) < w or (sy1 - sy0) < h * 0.8:
        return False
    if (sx1 - sx0) > 8 * max(w, h) or (sy1 - sy0) > 4 * max(w, h):
        return False  # a huge loop is not about this box
    if f.closure > 0.45 and f.length < 2.4 * max(sx1 - sx0, sy1 - sy0):
        return False
    return _point_in_polygon(((rect[0] + rect[2]) / 2, (rect[1] + rect[3]) / 2), stroke.points)


@dataclass
class AreaScore:
    area: AnswerArea
    ink: float  # path length inside the (slightly grown) box, in box sides
    circled: bool
    centred_ink: float = 0.0  # ink from strokes centred on this box
    strokes_in: int = 0  # separate strokes that put ink into the box

    @property
    def selected(self) -> bool:
        return self.circled or self.centred_ink >= 0.6

    @property
    def stray(self) -> bool:
        """Ink reaches the box, but the mark is centred elsewhere (between boxes)."""
        return not self.selected and self.ink >= 0.6

    @property
    def cancelled(self) -> bool:
        """Box filled solid, or a tick crossed out (3+ strokes): undone."""
        return self.ink >= 6.0 or self.strokes_in >= 3


def _centre(r: Rect) -> Tuple[float, float]:
    return (r[0] + r[2]) / 2, (r[1] + r[3]) / 2


def _encloses_label(stroke: Stroke, label: Rect) -> bool:
    """A loop drawn around the printed label text (not too big for it)."""
    from remarkable_mcp.workflows.ink.marks import _point_in_polygon

    f = stroke_features(stroke)
    sx0, sy0, sx1, sy1 = stroke.bbox
    lw, lh = label[2] - label[0], label[3] - label[1]
    if (
        f.inner > 0.15
        or (sx1 - sx0) < 0.6 * lw
        or (sx1 - sx0) > 2.2 * lw + 40
        or (sy1 - sy0) > 4 * lh
    ):
        return False
    return _point_in_polygon(_centre(label), stroke.points)


def _enclosure_owner(stroke: Stroke, areas: Sequence[AnswerArea]) -> Optional[AnswerArea]:
    """The one area a loop is drawn around - its box, or its printed label -
    choosing the enclosed area nearest the loop's centre."""
    inside = [
        a
        for a in areas
        if _encloses(stroke, a.rect)
        or (a.label_rect is not None and _encloses_label(stroke, a.label_rect))
    ]
    if not inside:
        return None
    cx = (stroke.bbox[0] + stroke.bbox[2]) / 2
    cy = (stroke.bbox[1] + stroke.bbox[3]) / 2
    return min(
        inside,
        key=lambda a: math.hypot(
            (a.rect[0] + a.rect[2]) / 2 - cx, (a.rect[1] + a.rect[3]) / 2 - cy
        ),
    )


def _grown(r: Rect, pad: float = 2.0) -> Rect:
    return (r[0] - pad, r[1] - pad, r[2] + pad, r[3] + pad)


def _mostly_in_one(stroke: Stroke, area: AnswerArea, areas: Sequence[AnswerArea]) -> bool:
    """A mark drawn off-centre (e.g. a big tick whose tip is in the box):
    a good part of the stroke is in this box and none of it in another."""
    length = stroke_features(stroke).length or 1.0
    if math.dist(stroke.points[0], stroke.points[-1]) > 0.9 * length:
        return False  # a straight line: a crossing-out or a pointer, not a tick
    if ink_length_in(_grown(area.rect), [stroke]) < 0.3 * length:
        return False
    if area.label_rect is not None and ink_length_in(area.label_rect, [stroke]) > 0.1 * length:
        return False  # runs over the option's label: struck through
    return not any(
        ink_length_in(_grown(b.rect), [stroke]) > 0.1 * length for b in areas if b is not area
    )


def score_areas(areas: Sequence[AnswerArea], pages: Dict[int, PageInk]) -> List[AreaScore]:
    """Ink and circling per answer area.

    A loop drawn around one option is credited to that option only: it neither
    counts as ink in the neighbouring boxes it crosses nor circles them.
    """
    by_page: Dict[int, List[AnswerArea]] = {}
    for a in areas:
        by_page.setdefault(a.page, []).append(a)
    owner: Dict[int, AnswerArea] = {}  # id(stroke) -> the area it encloses
    for pno, page_areas in by_page.items():
        page = pages.get(pno)
        for st in page.strokes if page else []:
            o = _enclosure_owner(st, page_areas)
            if o is not None:
                owner[id(st)] = o
    page_areas_of = by_page
    scores = []
    for a in areas:
        page = pages.get(a.page)
        strokes = page.strokes if page else []
        plain = [st for st in strokes if id(st) not in owner]
        side = max(a.rect[2] - a.rect[0], a.rect[3] - a.rect[1])
        grown = _grown(a.rect)
        ink = ink_length_in(grown, plain) / (side or 1)
        near = (
            a.rect[0] - 0.3 * side,
            a.rect[1] - 0.3 * side,
            a.rect[2] + 0.3 * side,
            a.rect[3] + 0.3 * side,
        )
        centred = [
            st
            for st in plain
            if _inside(_centre(st.bbox), near) or _mostly_in_one(st, a, page_areas_of[a.page])
        ]
        centred_ink = ink_length_in(grown, centred) / (side or 1)
        strokes_in = sum(1 for st in plain if ink_length_in(grown, [st]) / (side or 1) >= 0.3)
        circled = any(owner.get(id(st)) is a for st in strokes)
        scores.append(AreaScore(a, ink, circled, centred_ink, strokes_in))
    return scores


@dataclass
class FieldAnswer:
    field: dict
    value: object = None
    status: str = "empty"  # answered | empty | ambiguous | needs_transcription
    detail: Dict[str, object] = field(default_factory=dict)
    strokes: List[Stroke] = field(default_factory=list)  # write-in ink
    rect: Optional[Rect] = None
    page: Optional[int] = None


def read_answers(manifest: dict, pages: Dict[int, PageInk]) -> List[FieldAnswer]:
    """Resolve every field's value from the ink on ``pages`` (keyed by PDF page)."""
    areas = [
        AnswerArea(
            a["field"],
            a["option"],
            a["page"],
            tuple(a["rect"]),
            tuple(a["label_rect"]) if a.get("label_rect") else None,
        )
        for a in manifest["areas"]
    ]
    by_field: Dict[str, List[AreaScore]] = {}
    for s in score_areas(areas, pages):
        by_field.setdefault(s.area.field_id, []).append(s)

    answers: List[FieldAnswer] = []
    for f in manifest["fields"]:
        if f["type"] not in _ANSWER_TYPES:
            continue
        scores = by_field.get(f["id"], [])
        ans = FieldAnswer(field=f)
        if f["type"] == "checkbox":
            s = scores[0]
            ans.value = s.selected and not s.cancelled
            ans.status = "answered" if s.ink > 0.2 or s.circled else "empty"
            if s.cancelled:
                ans.detail["note"] = "box filled solid - read as unticked"
        elif f["type"] in ("choice", "multi", "scale"):
            picked = [s for s in scores if s.selected and not s.cancelled]
            values = [s.area.option for s in picked]
            if f["type"] == "multi":
                ans.value = values
                ans.status = "answered" if values else "empty"
            elif len(values) == 1:
                ans.value = int(values[0]) if f["type"] == "scale" else values[0]
                ans.status = "answered"
            elif len(values) > 1:
                # A circled option beats ticks; otherwise the most ink wins but is flagged.
                circled = [s for s in picked if s.circled]
                best = circled[0] if len(circled) == 1 else max(picked, key=lambda s: s.ink)
                ans.value = int(best.area.option) if f["type"] == "scale" else best.area.option
                ans.status = "ambiguous"
                ans.detail["candidates"] = values
            crossed = [s.area.option for s in scores if s.cancelled]
            if crossed:
                ans.detail["cancelled"] = crossed
            stray = [s.area.option for s in scores if s.stray and not s.cancelled]
            if stray and ans.status == "empty":
                # Ink reaches an option's box but the mark sits between options.
                ans.status = "ambiguous"
                ans.detail["ink_between"] = stray
        elif f["type"] == "text":
            s = scores[0]
            page = pages.get(s.area.page)
            if page:
                ink = [
                    st
                    for st in page.strokes
                    if not st.is_highlighter and _mostly_inside(st, s.area.rect)
                ]
                if ink:
                    ans.strokes = ink
                    ans.rect = _union_rect([st.bbox for st in ink])
                    ans.page = s.area.page
                    ans.status = "needs_transcription"
        answers.append(ans)
    return answers


def _mostly_inside(stroke: Stroke, rect: Rect) -> bool:
    inside = sum(1 for p in stroke.points if _inside(p, rect))
    return inside >= 0.6 * len(stroke.points)


def _union_rect(rects: Sequence[Rect]) -> Rect:
    return (
        min(r[0] for r in rects),
        min(r[1] for r in rects),
        max(r[2] for r in rects),
        max(r[3] for r in rects),
    )


def stray_strokes(manifest: dict, pages: Dict[int, PageInk]) -> Dict[int, List[Stroke]]:
    """Ink outside every answer area (margin remarks), per page."""
    grown: Dict[int, List[Rect]] = {}
    for a in manifest["areas"]:
        r = a["rect"]
        pad = 14 if a["option"] is not None else 4
        grown.setdefault(a["page"], []).append((r[0] - pad, r[1] - pad, r[2] + pad, r[3] + pad))
    areas_on: Dict[int, List[AnswerArea]] = {}
    for a in manifest["areas"]:
        areas_on.setdefault(a["page"], []).append(
            AnswerArea(a["field"], a["option"], a["page"], tuple(a["rect"]))
        )
    out: Dict[int, List[Stroke]] = {}
    for pno, page in pages.items():
        rects = grown.get(pno, [])
        extra = [
            s
            for s in page.strokes
            if not any(_mostly_inside(s, r) for r in rects)
            and _enclosure_owner(s, areas_on.get(pno, [])) is None  # circled answers
        ]
        if extra:
            out[pno] = extra
    return out
