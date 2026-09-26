"""Wireframe sketches -> an HTML prototype.

Builds on sketch recognition: shapes become UI elements, handwriting becomes
their labels, and nesting (a shape inside a bigger shape) becomes containment.
Conventions, as used in paper prototyping:

- box with an X across it       -> image placeholder
- small labelled box            -> button
- wide, flat box                -> text input (label = placeholder)
- box containing other elements -> container / card
- circle                        -> round button / icon
- loose handwriting             -> text (large writing -> heading)

The HTML keeps the sketch's layout (absolute positions scaled to a fixed
width), which is what you want from a first clickable prototype.
"""

from __future__ import annotations

import html
import math
from dataclasses import dataclass, field
from typing import List, Optional, Sequence

from remarkable_mcp.workflows.ink.page import Rect, Stroke
from remarkable_mcp.workflows.structure.sketch import Diagram, Label, recognise


@dataclass
class Element:
    id: str
    role: str  # container | image | button | input | box | text | heading
    rect: Rect
    label: Optional[str] = None
    label_ref: Optional[Label] = None
    children: List["Element"] = field(default_factory=list)
    parent: Optional[str] = None


def _area(r: Rect) -> float:
    return max(0.0, r[2] - r[0]) * max(0.0, r[3] - r[1])


def _inside(inner: Rect, outer: Rect, slack: float = 2.0) -> bool:
    return (
        inner[0] >= outer[0] - slack
        and inner[1] >= outer[1] - slack
        and inner[2] <= outer[2] + slack
        and inner[3] <= outer[3] + slack
    )


def _diagonals_in(rect: Rect, strokes: Sequence[Stroke]) -> int:
    """Straight diagonal strokes spanning most of ``rect`` (an X marks an image)."""
    w, h = rect[2] - rect[0], rect[3] - rect[1]
    n = 0
    for s in strokes:
        x0, y0, x1, y1 = s.bbox
        if not _inside(s.bbox, rect, 4):
            continue
        straight = math.dist(s.points[0], s.points[-1]) / (s.length or 1e-6)
        if straight > 0.85 and (x1 - x0) > 0.6 * w and (y1 - y0) > 0.6 * h:
            n += 1
    return n


def build_wireframe(strokes: Sequence[Stroke], region: Optional[Rect] = None) -> List[Element]:
    d: Diagram = recognise(strokes, region=region)
    pool = [s for s in strokes if not s.is_highlighter]
    elements: List[Element] = []
    for n in d.nodes:
        elements.append(Element(n.id, "box", n.shape.rect, label_ref=n.label))
    # Loose handwriting -> text elements.
    for k, lab in enumerate(d.free_text, start=1):
        h = lab.rect[3] - lab.rect[1]
        elements.append(Element(f"t{k}", "heading" if h > 16 else "text", lab.rect, label_ref=lab))

    # Nesting: parent = smallest box that contains the element.
    boxes = [e for e in elements if e.role == "box"]
    for e in elements:
        holders = [
            b
            for b in boxes
            if b is not e and _inside(e.rect, b.rect) and _area(b.rect) > _area(e.rect)
        ]
        if holders:
            parent = min(holders, key=lambda b: _area(b.rect))
            e.parent = parent.id
            parent.children.append(e)

    shapes_by_id = {n.id: n for n in d.nodes}
    for e in boxes:
        shape = shapes_by_id[e.id].shape
        w, h = e.rect[2] - e.rect[0], e.rect[3] - e.rect[1]
        if shape.kind == "ellipse":
            e.role = "button"
        elif _diagonals_in(e.rect, pool) >= 2:
            e.role = "image"
        elif e.children:
            e.role = "container"
        elif h < 40 and e.label_ref is not None and w < 170:
            e.role = "button"
        elif h < 40:
            e.role = "input"
    return sorted(elements, key=lambda e: (e.rect[1], e.rect[0]))


def to_html(elements: Sequence[Element], title: str = "Wireframe", width: int = 800) -> str:
    if not elements:
        return "<!doctype html><title>Wireframe</title><p>No elements recognised.</p>"
    x0 = min(e.rect[0] for e in elements)
    y0 = min(e.rect[1] for e in elements)
    x1 = max(e.rect[2] for e in elements)
    y1 = max(e.rect[3] for e in elements)
    scale = width / max(x1 - x0, 1.0)

    def box(e: Element) -> str:
        left, top = (e.rect[0] - x0) * scale, (e.rect[1] - y0) * scale
        w, h = (e.rect[2] - e.rect[0]) * scale, (e.rect[3] - e.rect[1]) * scale
        return f"left:{left:.0f}px;top:{top:.0f}px;width:{w:.0f}px;height:{h:.0f}px"

    parts = []
    for e in elements:
        label = html.escape(e.label or "")
        style = box(e)
        if e.role == "image":
            parts.append(
                f'<div class="wf img" style="{style}" data-id="{e.id}">{label or "image"}</div>'
            )
        elif e.role == "button":
            parts.append(
                f'<button class="wf" style="{style}" data-id="{e.id}">{label or "Button"}</button>'
            )
        elif e.role == "input":
            parts.append(
                f'<input class="wf" style="{style}" data-id="{e.id}" placeholder="{label}">'
            )
        elif e.role == "heading":
            parts.append(
                f'<h2 class="wf" style="{style}" data-id="{e.id}">{label or "Heading"}</h2>'
            )
        elif e.role == "text":
            parts.append(f'<p class="wf" style="{style}" data-id="{e.id}">{label or "Text"}</p>')
        else:
            cls = "card" if e.role == "container" else "box"
            parts.append(f'<div class="wf {cls}" style="{style}" data-id="{e.id}">{label}</div>')
    height = (y1 - y0) * scale + 20
    return f"""<!doctype html>
<html><head><meta charset="utf-8"><title>{html.escape(title)}</title>
<style>
body {{ font-family: system-ui, sans-serif; margin: 24px; background: #fafafa; }}
.canvas {{ position: relative; width: {width}px; height: {height:.0f}px; }}
.wf {{ position: absolute; box-sizing: border-box; margin: 0; font-size: 14px; }}
.box, .card {{ border: 1.5px solid #333; border-radius: 6px; padding: 6px; background: #fff; }}
.card {{ background: #f3f3f3; }}
.img {{ border: 1.5px dashed #777; display: flex; align-items: center; justify-content: center;
       color: #777;
       background: repeating-linear-gradient(45deg, #eee, #eee 8px, #f8f8f8 8px, #f8f8f8 16px); }}
button.wf {{ border: 1.5px solid #333; border-radius: 18px; background: #fff; cursor: pointer; }}
input.wf {{ border: 1.5px solid #333; border-radius: 4px; padding: 4px 8px; }}
h2.wf {{ font-size: 22px; }}
</style></head>
<body><div class="canvas">
{chr(10).join(parts)}
</div></body></html>
"""


def outline(elements: Sequence[Element]) -> List[dict]:
    return [
        {
            "id": e.id,
            "role": e.role,
            "label": e.label,
            "parent": e.parent,
            "rect": [round(v, 1) for v in e.rect],
        }
        for e in elements
    ]
