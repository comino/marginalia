"""MCP tools that turn ink into structure: tables, wireframes, math, daily digest."""

from __future__ import annotations

import asyncio
import io
import tempfile
import time
import zipfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import List, Optional

from mcp.types import ToolAnnotations

from remarkable_mcp.api import get_item_path, get_items_by_id
from remarkable_mcp.responses import make_error, make_response
from remarkable_mcp.workflows import cloud, handwriting, live
from remarkable_mcp.workflows.ink import load_document_ink_from_zip
from remarkable_mcp.workflows.marks import _cluster, _union, rect_distance
from remarkable_mcp.workflows.sketch import recognise
from remarkable_mcp.workflows.sketch_tools import _load_page
from remarkable_mcp.workflows.state import Store, now_iso
from remarkable_mcp.workflows.table import cell_crops, find_table
from remarkable_mcp.workflows.wireframe import build_wireframe, outline, to_html

_READ = ToolAnnotations(
    title="Read Structure from Ink",
    read_only_hint=True,
    destructive_hint=False,
    idempotent_hint=True,
    open_world_hint=False,
)
_DIGEST = ToolAnnotations(
    title="Daily Ink Digest",
    read_only_hint=False,  # remembers the last digest (local state)
    destructive_hint=False,
    idempotent_hint=False,
    open_world_hint=False,
)


def _region(region: Optional[List[float]]):
    if region is None:
        return None
    if len(region) != 4:
        raise ValueError("region must be [x0, y0, x1, y1]")
    return tuple(float(v) for v in region)


def _errors(exc: Exception, document: str) -> str:
    if isinstance(exc, LookupError) and not isinstance(exc, IndexError):
        return make_error(
            "document_not_found", f"Document not found: '{document}'", "Use remarkable_browse()."
        )
    if isinstance(exc, IndexError):
        return make_error("page_out_of_range", str(exc), "Pick an existing page.")
    if isinstance(exc, ValueError):
        return make_error("invalid_arguments", str(exc), "Check the parameters.")
    return make_error("failed", str(exc), "Check remarkable_status().")


async def remarkable_table(
    document: str, page: int = 1, region: Optional[List[float]] = None, include_images: bool = False
):
    """
    <usecase>Read a hand-drawn table (ruled rows and columns) into Markdown and CSV.</usecase>
    <instructions>
    Rows and columns come from the ruled lines (a box around the table counts);
    the handwriting in each cell is transcribed separately. The first row is
    treated as the header in the Markdown output. Cells with ink but no
    handwriting backend come back as null - use include_images=true to read them.
    </instructions>
    <parameters>
    - document: Document name or path.
    - page: 1-based page (default 1).
    - region: Optional [x0, y0, x1, y1] (page points) when the page has more than a table.
    - include_images: Attach a crop per non-empty cell.
    </parameters>
    """

    def work():
        pg = _load_page(document, page)
        t = find_table(pg.strokes, _region(region))
        if t is None:
            return None, []
        crops = cell_crops(t)
        pngs = [handwriting.render_strokes_png(s, r) for _, _, s, r in crops]
        texts = handwriting.transcribe_many(pngs, strokes=[s for _, _, s, _ in crops])
        for (r, c, _, _), (text, _) in zip(crops, texts):
            t.text[r][c] = text
        return t, [(f"r{r + 1}c{c + 1}", "cell", png) for (r, c, _, _), png in zip(crops, pngs)]

    try:
        t, images = await asyncio.to_thread(work)
    except Exception as exc:
        return _errors(exc, document)
    if t is None:
        return make_response(
            {"table": None},
            "No table found: draw straight row and column lines (or a box with inner lines).",
        )
    rows, cols = t.shape
    empty_cells_with_ink = sum(
        1 for r in range(rows) for c in range(cols) if t.cells[r][c] and t.text[r][c] is None
    )
    payload = make_response(
        {
            "rows": rows,
            "columns": cols,
            "cells": t.text,
            "markdown": t.to_markdown(),
            "csv": t.to_csv(),
        },
        f"{rows}x{cols} table."
        + (
            f" {empty_cells_with_ink} cell(s) not transcribed; include_images=true shows them."
            if empty_cells_with_ink
            else ""
        ),
    )
    return cloud.with_images(payload, images) if include_images and images else payload


async def remarkable_wireframe(
    document: str, page: int = 1, region: Optional[List[float]] = None, include_images: bool = False
):
    """
    <usecase>Turn a paper wireframe into an HTML prototype.</usecase>
    <instructions>
    Boxes, circles and handwriting become UI elements (image = box with an X,
    button = small labelled box or circle, input = wide flat box, container =
    box holding other elements, text/heading = loose handwriting). Returns a
    self-contained HTML page that keeps the sketch's layout, plus an outline
    of the elements with their roles and nesting - refine it in real code.
    </instructions>
    <parameters>
    - document: Document name or path.
    - page: 1-based page (default 1).
    - region: Optional [x0, y0, x1, y1] (page points).
    - include_images: Attach a render of the prototype.
    </parameters>
    """

    def work():
        pg = _load_page(document, page)
        elements = build_wireframe(pg.strokes, _region(region))
        labelled = [e for e in elements if e.label_ref is not None]
        crops = [
            handwriting.render_strokes_png(e.label_ref.strokes, e.label_ref.rect) for e in labelled
        ]
        texts = handwriting.transcribe_many(crops, strokes=[e.label_ref.strokes for e in labelled])
        for e, (text, _) in zip(labelled, texts):
            e.label = text
        return elements

    try:
        elements = await asyncio.to_thread(work)
    except Exception as exc:
        return _errors(exc, document)
    page_html = to_html(elements, title=f"{document} p{page}")
    payload = make_response(
        {"elements": outline(elements), "html": page_html},
        f"{len(elements)} element(s). Save 'html' to a file to click through it.",
    )
    if include_images:
        png = _html_preview(page_html)
        if png:
            return cloud.with_images(payload, [("prototype", "render", png)])
    return payload


def _html_preview(page_html: str) -> Optional[bytes]:
    import pymupdf

    try:
        with cloud.MUPDF_LOCK:
            story = pymupdf.Story(html=page_html)
            buf = io.BytesIO()
            writer = pymupdf.DocumentWriter(buf)
            rect = pymupdf.Rect(0, 0, 900, 1200)
            more = 1
            while more:
                dev = writer.begin_page(rect)
                more, _ = story.place(rect)
                story.draw(dev)
                writer.end_page()
            writer.close()
            with pymupdf.open("pdf", buf.getvalue()) as doc:
                return doc[0].get_pixmap(dpi=60).tobytes("png")
    except Exception:
        return None


async def remarkable_math(
    document: str, page: int = 1, region: Optional[List[float]] = None, include_images: bool = False
):
    """
    <usecase>Convert handwritten mathematics into LaTeX.</usecase>
    <instructions>
    Uses MyScript's math recogniser (from the strokes) when configured, else
    Claude vision with a LaTeX prompt. Groups the ink into blocks (separate
    equations) and returns LaTeX per block. Without a backend the blocks come
    back as crops (include_images=true) for you to read.
    </instructions>
    <parameters>
    - document: Document name or path.
    - page: 1-based page (default 1).
    - region: Optional [x0, y0, x1, y1] (page points).
    - include_images: Attach a crop per block.
    </parameters>
    """

    def work():
        pg = _load_page(document, page)
        strokes = [s for s in pg.strokes if not s.is_highlighter]
        reg = _region(region)
        if reg is not None:
            strokes = [s for s in strokes if rect_distance(s.bbox, reg) == 0]
        blocks = []
        for g in _cluster([s.bbox for s in strokes], 14.0, 10.0):
            group = sorted((strokes[i] for i in g), key=lambda s: s.index)
            blocks.append((group, _union([s.bbox for s in group])))
        blocks.sort(key=lambda b: (b[1][1], b[1][0]))
        pngs = [handwriting.render_strokes_png(g, r) for g, r in blocks]
        texts = handwriting.transcribe_many(pngs, strokes=[g for g, _ in blocks], mode="math")
        return blocks, pngs, texts

    try:
        blocks, pngs, texts = await asyncio.to_thread(work)
    except Exception as exc:
        return _errors(exc, document)
    out = [
        {"block": n, "rect": [round(v, 1) for v in r], "latex": t, "engine": e}
        for n, ((_, r), (t, e)) in enumerate(zip(blocks, texts), start=1)
    ]
    missing = sum(1 for b in out if b["latex"] is None)
    payload = make_response(
        {"blocks": out},
        f"{len(out)} block(s)."
        + (
            " No math backend: configure MyScript or ANTHROPIC_API_KEY, or use include_images=true."
            if missing
            else ""
        ),
    )
    if include_images:
        return cloud.with_images(
            payload, [(f"block {b['block']}", "math", p) for b, p in zip(out, pngs)]
        )
    return payload


# --------------------------------------------------------------------------- digest


def _digest_store() -> Store:
    return Store("ink-digest")


MAX_DIGEST_PAGES = 6


async def remarkable_ink_digest(
    since_hours: float = 24.0,
    max_documents: int = 8,
    include_images: bool = False,
    mark_seen: bool = True,
):
    """
    <usecase>What did I write on the tablet recently? A per-page digest of new ink.</usecase>
    <instructions>
    Finds documents modified in the last `since_hours`, works out which pages
    got new strokes since the previous digest (page-level stroke hashes), and
    returns for each such page its handwriting (transcribed per block when a
    backend is configured) and whether it holds drawings. Good for a daily
    summary or for routing notes to projects. Documents generated by the
    workflow tools are included - ink on them is ink too.
    </instructions>
    <parameters>
    - since_hours: Look-back window (default 24).
    - max_documents: Most recently modified documents to include (default 8).
    - include_images: Attach a crop per text block.
    - mark_seen: Remember the reported pages (default true). Only pages that
      were actually reported are remembered; the rest come in the next digest.
      Use false to peek (e.g. again with include_images=true).
    </parameters>
    """
    store = _digest_store()
    previous = (store.get("last") or {}).get("pages", {})

    def work():
        c = cloud.client()
        cloud.refresh(c)
        items = c.get_meta_items()
        by_id = get_items_by_id(items)
        snap = live.snapshot(c)
        cutoff = datetime.now(timezone.utc) - timedelta(hours=since_hours)

        def modified(it):
            lm = getattr(it, "last_modified", None) or getattr(it, "ModifiedClient", None)
            if lm is None:
                return None
            # Naive timestamps from the sync client are local time.
            return lm if lm.tzinfo else lm.astimezone(timezone.utc)

        recent = [
            it
            for it in items
            if not it.is_folder
            and not cloud.is_trashed(it, by_id)
            and (m := modified(it)) is not None
            and m >= cutoff
            and snap.get(it.ID)
            and any(k != "*" for k in snap[it.ID].pages)
        ]
        recent.sort(key=lambda it: modified(it), reverse=True)
        docs_out, images = [], []
        reported: dict = {}  # doc id -> {stroke file id: hash} actually reported
        skipped_docs = 0
        for it in recent:
            if len(docs_out) >= max_documents:
                skipped_docs += 1
                continue
            pages_now = snap[it.ID].pages
            before = previous.get(it.ID, {})
            changed_ids = [
                fid.rsplit("/", 1)[-1].removesuffix(".rm")
                for fid, h in pages_now.items()
                if before.get(fid) != h
            ]
            if not changed_ids:
                continue
            zip_bytes = cloud.download_zip(c, it)
            with tempfile.TemporaryDirectory() as tmp:
                with zipfile.ZipFile(io.BytesIO(zip_bytes)) as zf:
                    zf.extractall(tmp)
                from remarkable_mcp.extract import _get_page_order

                order = _get_page_order(Path(tmp))
            all_numbers = sorted({order.index(p) + 1 for p in changed_ids if p in order})
            numbers = all_numbers[:MAX_DIGEST_PAGES]
            done_ids = {order[n - 1] for n in numbers}
            reported[it.ID] = {
                fid: h
                for fid, h in pages_now.items()
                if fid.rsplit("/", 1)[-1].removesuffix(".rm") in done_ids
            }
            with cloud.MUPDF_LOCK:
                ink = load_document_ink_from_zip(zip_bytes, numbers)
            pages_out = []
            for pg in ink.pages:
                strokes = [s for s in pg.strokes if not s.is_highlighter]
                if not strokes:
                    continue
                groups = _cluster([s.bbox for s in strokes], 16.0, 12.0)
                blocks = []
                drawings = 0
                for g in groups:
                    group = sorted((strokes[i] for i in g), key=lambda s: s.index)
                    if recognise(group).is_diagram:
                        drawings += 1
                        continue
                    blocks.append((group, _union([s.bbox for s in group])))
                blocks.sort(key=lambda b: (b[1][1], b[1][0]))
                pngs = [handwriting.render_strokes_png(g, r) for g, r in blocks]
                texts = handwriting.transcribe_many(pngs, strokes=[g for g, _ in blocks])
                pages_out.append(
                    {"page": pg.page, "text": [t for t, _ in texts], "drawings": drawings}
                )
                if include_images:
                    images += [(f"{it.VissibleName} p{pg.page}", "block", p) for p in pngs]
            if pages_out:
                entry = {
                    "document": it.VissibleName,
                    "path": get_item_path(it, by_id),
                    "modified": modified(it).isoformat(timespec="minutes"),
                    "pages": pages_out,
                }
                if len(all_numbers) > len(numbers):
                    entry["more_pages"] = len(all_numbers) - len(numbers)
                docs_out.append(entry)
        return reported, docs_out, images, skipped_docs

    try:
        reported, docs_out, images, skipped_docs = await asyncio.to_thread(work)
    except Exception as exc:
        return make_error("digest_failed", str(exc), "Check remarkable_status().")

    if mark_seen:

        def remember(cur):
            cur = cur or {"pages": {}}
            for doc_id, pages in reported.items():
                cur["pages"].setdefault(doc_id, {}).update(pages)
            cur["at"] = now_iso()
            return cur

        store.update("last", remember)
    untranscribed = sum(1 for d in docs_out for p in d["pages"] for t in p["text"] if t is None)
    hint = f"{len(docs_out)} document(s) with new ink since the last digest."
    if skipped_docs or any(d.get("more_pages") for d in docs_out):
        hint += " More new ink remains; call again to continue."
    if untranscribed and not include_images:
        hint += f" {untranscribed} text block(s) untranscribed; include_images=true shows them."
    payload = make_response(
        {
            "since_hours": since_hours,
            "generated": time.strftime("%Y-%m-%d %H:%M"),
            "documents": docs_out,
        },
        hint,
    )
    return cloud.with_images(payload, images) if include_images and images else payload


def register(mcp, write_enabled: bool) -> None:
    del write_enabled
    mcp.tool(annotations=_READ)(remarkable_table)
    mcp.tool(annotations=_READ)(remarkable_wireframe)
    mcp.tool(annotations=_READ)(remarkable_math)
    mcp.tool(annotations=_DIGEST)(remarkable_ink_digest)
