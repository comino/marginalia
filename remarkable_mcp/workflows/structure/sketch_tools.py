"""MCP tools: sketches -> diagrams, and page ink -> regions."""

from __future__ import annotations

import asyncio
from typing import List, Optional

from mcp.types import ToolAnnotations

from remarkable_mcp.core.responses import make_error, make_response
from remarkable_mcp.transports.api import get_items_by_id
from remarkable_mcp.workflows import cloud
from remarkable_mcp.workflows.ink import handwriting
from remarkable_mcp.workflows.ink.marks import _cluster, _union
from remarkable_mcp.workflows.ink.page import PageInk, load_document_ink_from_zip
from remarkable_mcp.workflows.structure.sketch import recognise, summary, to_mermaid, to_svg

_READ = ToolAnnotations(
    title="Interpret reMarkable Sketch",
    read_only_hint=True,
    destructive_hint=False,
    idempotent_hint=True,
    open_world_hint=False,
)


def _load_page(document: str, page: int) -> PageInk:
    from remarkable_mcp.core.tools import _find_target_document

    c = cloud.client()
    items = c.get_meta_items()
    doc = _find_target_document(items, get_items_by_id(items), document)
    if doc is None:
        raise LookupError(document)
    zip_bytes = cloud.download_zip(c, doc)
    with cloud.MUPDF_LOCK:
        ink = load_document_ink_from_zip(zip_bytes, [page])
    if not ink.pages:
        raise IndexError(f"Page {page} does not exist (document has {ink.page_count} pages).")
    return ink.pages[0]


def _svg_png(svg: str) -> Optional[bytes]:
    import pymupdf

    try:
        with cloud.MUPDF_LOCK, pymupdf.open(stream=svg.encode(), filetype="svg") as doc:
            return doc[0].get_pixmap(dpi=110).tobytes("png")
    except Exception:
        return None


async def remarkable_sketch(
    document: str,
    page: int = 1,
    region: Optional[List[float]] = None,
    include_svg: bool = False,
    include_images: bool = False,
):
    """
    <usecase>Turn a hand-drawn diagram (boxes, circles, diamonds, arrows) into a graph.</usecase>
    <instructions>
    Recognises shapes geometrically: rectangles, ellipses, diamonds and
    triangles become nodes; lines and arrows become edges attached to the
    shapes they touch; handwriting inside a shape labels it, handwriting next
    to a connector labels the edge. Returns nodes/edges JSON and a Mermaid
    flowchart (labels transcribed when a handwriting backend is configured,
    otherwise node ids are used and include_images=true shows the labels).
    include_svg=true adds a clean vector redraw. Use remarkable_regions first
    when the page mixes a diagram with notes, then pass the diagram's region.
    </instructions>
    <parameters>
    - document: Document name or path.
    - page: 1-based page (default 1).
    - region: Optional [x0, y0, x1, y1] in page points to restrict to one area.
    - include_svg: Include the redrawn diagram as SVG text.
    - include_images: Attach a PNG of the redrawn diagram and of each label.
    </parameters>
    <examples>
    - remarkable_sketch("Architecture ideas", page=2)
    - remarkable_sketch("Meeting notes", page=1, region=[40, 300, 420, 560], include_images=True)
    </examples>
    """
    if region is not None and len(region) != 4:
        return make_error(
            "invalid_region", "region must be [x0, y0, x1, y1].", "Omit it for the whole page."
        )

    def work():
        pg = _load_page(document, page)
        d = recognise(pg.strokes, region=tuple(region) if region else None)
        labels = d.labels()
        crops = [handwriting.render_strokes_png(lab.strokes, lab.rect) for lab in labels]
        texts = handwriting.transcribe_many(
            crops, handwriting.backend(), strokes=[lab.strokes for lab in labels]
        )
        for lab, (text, _engine) in zip(labels, texts):
            lab.text = text
        return pg, d, list(zip(labels, crops))

    try:
        pg, d, label_crops = await asyncio.to_thread(work)
    except IndexError as exc:  # before LookupError: IndexError is a subclass
        return make_error("page_out_of_range", str(exc), "Pick an existing page.")
    except LookupError:
        return make_error(
            "document_not_found", f"Document not found: '{document}'", "Use remarkable_browse()."
        )
    except Exception as exc:
        return make_error("sketch_failed", str(exc), "Check remarkable_status().")

    if not d.is_diagram:
        return make_response(
            {"document": document, "page": page, "nodes": [], "edges": []},
            "No diagram shapes found on this page"
            + (" in that region." if region else ". Try remarkable_regions to locate drawings."),
        )
    data = {"document": document, "page": page, **summary(d), "mermaid": to_mermaid(d)}
    svg = to_svg(d)
    if include_svg:
        data["svg"] = svg
    untranscribed = sum(1 for lab in d.labels() if lab.text is None)
    hint = f"{len(d.nodes)} node(s), {len(d.edges)} edge(s)."
    if untranscribed:
        hint += f" {untranscribed} label(s) not transcribed; include_images=true shows them."
    payload = make_response(data, hint)
    if not include_images:
        return payload
    images = []
    png = _svg_png(svg)
    if png:
        images.append(("diagram", "clean redraw", png))
    for n, (lab, crop) in enumerate(label_crops, start=1):
        owner = next((x.id for x in d.nodes if x.label is lab), None) or next(
            (f"{e.source}->{e.target}" for e in d.edges if e.label is lab), f"text{n}"
        )
        images.append((owner, "label", crop))
    return cloud.with_images(payload, images)


async def remarkable_regions(
    document: str,
    page: int = 1,
    include_images: bool = False,
):
    """
    <usecase>Split a page's ink into separate regions (text blocks, drawings).</usecase>
    <instructions>
    Groups nearby strokes into regions and labels each "writing" or
    "drawing" (a region with diagram shapes). Writing regions are transcribed
    when a handwriting backend is configured. Each region has a rect in page
    points you can pass to remarkable_sketch(region=...). include_images=true
    returns one small crop per region - much easier for a vision model than a
    whole page.
    </instructions>
    <parameters>
    - document: Document name or path.
    - page: 1-based page (default 1).
    - include_images: Attach a PNG crop per region.
    </parameters>
    """

    def work():
        pg = _load_page(document, page)
        strokes = [s for s in pg.strokes if not s.is_highlighter]
        regions = []
        if strokes:
            for g in _cluster([s.bbox for s in strokes], 16.0, 14.0):
                group = sorted((strokes[i] for i in g), key=lambda s: s.index)
                rect = _union([s.bbox for s in group])
                d = recognise(group)
                kind = "drawing" if d.is_diagram else "writing"
                regions.append((kind, rect, group))
        regions.sort(key=lambda r: (round(r[1][1] / 20), r[1][0]))
        crops = [handwriting.render_strokes_png(g, r) for _, r, g in regions]
        writing = [i for i, (k, _, _) in enumerate(regions) if k == "writing"]
        texts = handwriting.transcribe_many(
            [crops[i] for i in writing],
            handwriting.backend(),
            strokes=[regions[i][2] for i in writing],
        )
        text_by_index = {i: t for i, (t, _) in zip(writing, texts)}
        return pg, regions, crops, text_by_index

    try:
        pg, regions, crops, texts = await asyncio.to_thread(work)
    except IndexError as exc:  # before LookupError: IndexError is a subclass
        return make_error("page_out_of_range", str(exc), "Pick an existing page.")
    except LookupError:
        return make_error(
            "document_not_found", f"Document not found: '{document}'", "Use remarkable_browse()."
        )
    except Exception as exc:
        return make_error("regions_failed", str(exc), "Check remarkable_status().")

    out = []
    for i, (kind, rect, group) in enumerate(regions, start=1):
        item = {
            "id": f"r{i}",
            "kind": kind,
            "rect": [round(v, 1) for v in rect],
            "strokes": len(group),
        }
        if kind == "writing":
            item["text"] = texts.get(i - 1)
        out.append(item)
    payload = make_response(
        {
            "document": document,
            "page": page,
            "page_size": [round(pg.width, 1), round(pg.height, 1)],
            "regions": out,
        },
        f"{len(out)} region(s). Use remarkable_sketch(document, page, region=rect) on drawings.",
    )
    if include_images and out:
        return cloud.with_images(
            payload, [(r["id"], r["kind"], crops[n]) for n, r in enumerate(out)]
        )
    return payload


def register(mcp, write_enabled: bool) -> None:
    del write_enabled  # read-only tools
    mcp.tool(annotations=_READ)(remarkable_sketch)
    mcp.tool(annotations=_READ)(remarkable_regions)
