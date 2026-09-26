"""MCP tools for following the tablet live."""

from __future__ import annotations

import asyncio
import io
import tempfile
import time
import zipfile
from dataclasses import replace
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
                item["diagram"] = d if d.is_diagram else None  # labels read after the lock

            if include_images:
                png, _ = render_merged_page_from_extracted_document(
                    Path(tmp), page.page, canvas_width=700, canvas_height=933
                )
                if png:
                    images.append((f"page {page.page}", "current page", png))
            pages_out.append(item)
    # Transcription can call a network OCR backend: do it without holding the
    # MuPDF lock, which every render tool shares.
    for item in pages_out:
        d = item.get("diagram")
        if d is None:
            continue
        labels = d.labels()
        crops = [handwriting.render_strokes_png(lab.strokes, lab.rect) for lab in labels]
        texts = handwriting.transcribe_many(crops, strokes=[lab.strokes for lab in labels])
        for lab, (text, _) in zip(labels, texts):
            lab.text = text
        item["diagram"] = {**summary(d), "mermaid": to_mermaid(d)}
    return pages_out, images


async def remarkable_live_watch(
    document: Optional[str] = None,
    timeout: int = 90,
    settle: float = 4.0,
    analyse: str = "auto",
    include_images: bool = True,
    since: Optional[int] = None,
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
    Every answer carries a "cursor". Pass it back as `since` on the next call
    and nothing that happened in between is missed - that is how to follow a
    live sketching session. Returns status "no_change" after `timeout`
    seconds; just call again with the same cursor.
    </instructions>
    <parameters>
    - document: Name, path or id to follow (default: any document).
    - timeout: Seconds to wait for a change (default 90, max 600).
    - settle: Seconds of quiet after a change before answering (default 4).
    - analyse: "auto" | "annotations" | "sketch" | "none".
    - include_images: Attach renders of the changed pages (default true).
    - since: Cursor from the previous call (default: only changes from now on).
    </parameters>
    <examples>
    - remarkable_live_watch("Whiteboard")                  # first call
    - remarkable_live_watch("Whiteboard", since=17)        # continue from cursor 17
    </examples>
    """
    timeout = max(5, min(int(timeout), 600))
    try:
        watcher = await live.shared_watcher()
    except Exception as exc:
        return make_error("watch_failed", str(exc), "Check remarkable_status().")

    def wanted(ch: live.Change) -> bool:
        return ch.kind in ("ink", "new") and _matches(ch, document)

    start_seq = watcher.seq if since is None else int(since)
    gap = False
    oldest = watcher.history[0][0] if getattr(watcher, "history", None) else None
    if since is not None and (
        start_seq > watcher.seq or (oldest is not None and start_seq < oldest - 1)
    ):
        # The cursor is from before a server restart, or older than the kept
        # history: resume from what is available and say so.
        gap = True
        start_seq = (oldest - 1) if oldest is not None else watcher.seq
    deadline = time.time() + timeout
    batch: List[live.Change] = []
    cursor = start_seq
    while time.time() < deadline:
        pending = [(n, ch) for n, ch in watcher.changes_since(cursor) if wanted(ch)]
        if pending:
            batch += [ch for _, ch in pending]
            cursor = max(cursor, watcher.seq)
            # Keep collecting until the user pauses for `settle` seconds (bounded
            # by the deadline plus one settle period).
            quiet_until = time.time() + settle
            while time.time() < min(quiet_until, deadline + settle):
                await asyncio.sleep(0.25)
                more = [ch for n, ch in watcher.changes_since(cursor) if wanted(ch)]
                if more:
                    batch += more
                    cursor = max(cursor, watcher.seq)
                    quiet_until = time.time() + settle
            break
        cursor = max(cursor, watcher.seq)  # skip unrelated changes
        await asyncio.sleep(0.5)

    status = {
        "watch_mode": watcher.mode,
        "syncs_seen": watcher.events_seen,
        "cursor": max(cursor, start_seq),
    }
    if gap:
        status["gap"] = True  # some changes before this point may have been missed
    if not batch:
        return make_response(
            {"status": "no_change", **status},
            f"Nothing changed yet. Call again with since={status['cursor']} to keep following.",
        )

    merged: dict = {}
    for ch in batch:
        if ch.doc_id not in merged:
            merged[ch.doc_id] = replace(ch, pages=list(ch.pages))  # never mutate shared events
        else:
            entry = merged[ch.doc_id]
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
        f"Call remarkable_live_watch again with since={status['cursor']} to keep following"
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
        "socket = push notifications; polling = fallback (60 s, backing off to 5 min).",
    )


def register(mcp, write_enabled: bool) -> None:
    del write_enabled
    mcp.tool(annotations=_READ)(remarkable_live_watch)
    mcp.tool(annotations=_READ)(remarkable_live_status)
