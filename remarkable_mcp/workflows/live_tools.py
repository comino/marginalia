"""MCP tools for following the tablet live."""

from __future__ import annotations

import asyncio
import io
import tempfile
import time
import zipfile
from pathlib import Path
from typing import List, Optional

from mcp.types import ToolAnnotations

from remarkable_mcp.responses import make_error, make_response
from remarkable_mcp.workflows import cloud, handwriting, live
from remarkable_mcp.workflows.ink import load_document_ink_from_zip
from remarkable_mcp.workflows.review import collect_requests
from remarkable_mcp.workflows.sketch import recognise, summary, to_mermaid

_READ = ToolAnnotations(
    title="Watch the Tablet Live",
    read_only_hint=True,
    destructive_hint=False,
    idempotent_hint=False,
    open_world_hint=False,
)


def _matches(change: live.Change, document: Optional[str]) -> bool:
    if not document:
        return True
    d = document.strip().lower().strip("/")
    return d in (change.doc_id.lower(), change.name.lower(), change.path.lower().strip("/"))


def _analyse(doc_id: str, page_ids: List[str], mode: str, include_images: bool):
    """Download the document once and describe the changed pages."""
    from remarkable_mcp.extract import _get_page_order, render_merged_page_from_extracted_document

    c = cloud.client()
    doc = cloud.find_by_id(c, doc_id)
    if doc is None:
        return [], []
    zip_bytes = cloud.download_zip(c, doc)
    pages_out, images = [], []
    with tempfile.TemporaryDirectory() as tmp, cloud.MUPDF_LOCK:
        with zipfile.ZipFile(io.BytesIO(zip_bytes)) as zf:
            zf.extractall(tmp)
        order = _get_page_order(Path(tmp))
        numbers = sorted({order.index(p) + 1 for p in page_ids if p in order})
        ink = load_document_ink_from_zip(zip_bytes, numbers)
        for page in ink.pages:
            item: dict = {"page": page.page, "strokes": len(page.strokes)}
            use = mode
            if use == "auto":
                use = "annotations" if page.pdf_page is not None and page.words else "sketch"
            if use == "annotations":
                reqs = collect_requests(type(ink)([page], ink.pdf_bytes, ink.page_count))
                item["marks"] = [r.to_dict(None, "none") for r in reqs]
            if use in ("sketch", "regions"):
                d = recognise(page.strokes)
                if d.is_diagram:
                    labels = d.labels()
                    crops = [
                        handwriting.render_strokes_png(lab.strokes, lab.rect) for lab in labels
                    ]
                    texts = handwriting.transcribe_many(
                        crops, strokes=[lab.strokes for lab in labels]
                    )
                    for lab, (text, _) in zip(labels, texts):
                        lab.text = text
                    item["diagram"] = {**summary(d), "mermaid": to_mermaid(d)}
                else:
                    item["diagram"] = None
            if include_images:
                png, _ = render_merged_page_from_extracted_document(
                    Path(tmp), page.page, canvas_width=700, canvas_height=933
                )
                if png:
                    images.append((f"page {page.page}", "current page", png))
            pages_out.append(item)
    return pages_out, images


async def remarkable_live_watch(
    document: Optional[str] = None,
    timeout: int = 90,
    settle: float = 4.0,
    analyse: str = "auto",
    include_images: bool = True,
):
    """
    <usecase>Wait for the user to write or draw on the tablet, then see what changed.</usecase>
    <instructions>
    Follows the tablet near-live: the reMarkable cloud notifies every sync (the
    tablet syncs every few seconds while you write). Blocks until ink changes -
    on `document` if given, else anywhere - waits `settle` seconds for the
    user to pause, then returns the changed pages with:
    - "annotations" on PDFs (strikes, circles, notes anchored to text),
    - "sketch" on notebooks (diagram nodes/edges + Mermaid when it is a diagram),
    - a render of each changed page (include_images=true).
    Call it again in a loop to follow a live sketching session. Returns
    status "no_change" after `timeout` seconds - just call again.
    </instructions>
    <parameters>
    - document: Name, path or id to follow (default: any document).
    - timeout: Seconds to wait for a change (default 90, max 600).
    - settle: Seconds of quiet after a change before answering (default 4).
    - analyse: "auto" | "annotations" | "sketch" | "none".
    - include_images: Attach renders of the changed pages (default true).
    </parameters>
    <examples>
    - remarkable_live_watch("Whiteboard")          # follow one notebook
    - remarkable_live_watch(timeout=300, analyse="none")  # what is the user touching?
    </examples>
    """
    timeout = max(5, min(int(timeout), 600))
    try:
        watcher = await live.shared_watcher()
    except Exception as exc:
        return make_error("watch_failed", str(exc), "Check remarkable_status().")
    queue = watcher.subscribe()
    try:
        deadline = time.time() + timeout
        batch: List[live.Change] = []
        while time.time() < deadline:
            try:
                ch = await asyncio.wait_for(queue.get(), timeout=max(0.1, deadline - time.time()))
            except asyncio.TimeoutError:
                break
            if ch.kind in ("ink", "new") and _matches(ch, document):
                batch.append(ch)
                # Collect everything until the user pauses for `settle` seconds.
                while True:
                    try:
                        more = await asyncio.wait_for(queue.get(), timeout=settle)
                    except asyncio.TimeoutError:
                        break
                    if more.kind in ("ink", "new") and _matches(more, document):
                        batch.append(more)
                break
    finally:
        watcher.unsubscribe(queue)

    status = {"watch_mode": watcher.mode, "syncs_seen": watcher.events_seen}
    if not batch:
        return make_response(
            {"status": "no_change", **status},
            "Nothing changed yet. Call remarkable_live_watch again to keep following.",
        )

    merged: dict = {}
    for ch in batch:
        entry = merged.setdefault(ch.doc_id, ch)
        if entry is not ch:
            entry.pages = sorted(set(entry.pages) | set(ch.pages))
    changes = list(merged.values())
    result = {"status": "changed", **status, "changes": [c.to_dict() for c in changes]}
    images: list = []
    if analyse != "none" and len(changes) == 1 and changes[0].pages:
        try:
            pages, images = await asyncio.to_thread(
                _analyse, changes[0].doc_id, changes[0].pages, analyse, include_images
            )
            result["pages"] = pages
        except Exception as exc:
            result["analysis_error"] = str(exc)
    payload = make_response(
        result,
        "Call remarkable_live_watch again to keep following"
        + (f" '{document}'." if document else " the tablet."),
    )
    return cloud.with_images(payload, images) if images else payload


async def remarkable_live_status() -> str:
    """
    <usecase>Show whether the live change stream is connected.</usecase>
    """
    w = live._shared
    if w is None:
        return make_response(
            {"running": False}, "Not started; remarkable_live_watch starts it on first use."
        )
    return make_response(
        {
            "running": True,
            "mode": w.mode,
            "syncs_seen": w.events_seen,
            "last_sync": time.strftime("%H:%M:%S", time.localtime(w.last_event))
            if w.last_event
            else None,
            "documents_tracked": len(w.state or {}),
            "refresh_errors": w.errors_total,
            "last_error": w.last_error,
        },
        "socket = push notifications; polling = fallback every 20s.",
    )


def register(mcp, write_enabled: bool) -> None:
    del write_enabled
    mcp.tool(annotations=_READ)(remarkable_live_watch)
    mcp.tool(annotations=_READ)(remarkable_live_status)
