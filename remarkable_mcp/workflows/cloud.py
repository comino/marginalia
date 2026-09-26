"""Transport glue shared by the workflow tools.

Tool modules call these through the module (``cloud.client()``), never via
``from ... import``, so tests can swap the whole transport in one place.
"""

from __future__ import annotations

import base64
import hashlib
import logging
import threading
from typing import Optional, Sequence, Tuple

from mcp.types import ImageContent, TextContent

from remarkable_mcp.api import get_item_path, get_items_by_id, get_rmapi

logger = logging.getLogger(__name__)

# A document counts as "done" once it sits in a folder with one of these names.
DONE_FOLDER_NAMES = {"reviewed", "done", "erledigt", "answered", "beantwortet"}

# MuPDF is not thread-safe; every workflow render/analysis runs under this lock.
MUPDF_LOCK = threading.Lock()


def client():
    return get_rmapi()


def is_cloud() -> bool:
    from remarkable_mcp.write_tools import _is_cloud_mode

    return _is_cloud_mode()


def refresh(c) -> None:
    """Drop cached metadata so the next listing reflects tablet-side changes."""
    from remarkable_mcp.write_tools import _invalidate_client_cache

    try:
        _invalidate_client_cache(c)
    except Exception as exc:  # stale metadata is only an optimisation problem
        logger.debug("Could not invalidate client cache: %s", exc)


def is_trashed(item, by_id: dict) -> bool:
    """True if the item or any ancestor sits in the tablet's trash."""
    seen = set()
    while item is not None and item.ID not in seen:
        seen.add(item.ID)
        parent = getattr(item, "Parent", "") or ""
        if parent == "trash" or getattr(item, "deleted", False):
            return True
        item = by_id.get(parent)
    return False


def find_by_id(c, doc_id: str):
    """The live (not trashed) document with this id, or None."""
    items = c.get_meta_items()
    by_id = get_items_by_id(items)
    doc = by_id.get(doc_id)
    return None if doc is None or is_trashed(doc, by_id) else doc


def ensure_folder(c, path: str) -> str:
    """Resolve ``/A/B`` to a folder id, creating missing levels (trash ignored)."""
    parent_id = ""
    for part in [p for p in path.strip("/").split("/") if p]:
        items = c.get_meta_items()
        by_id = get_items_by_id(items)
        found = next(
            (
                i.ID
                for i in items
                if i.is_folder
                and (getattr(i, "Parent", "") or "") == parent_id
                and i.VissibleName.lower() == part.lower()
                and not is_trashed(i, by_id)
            ),
            None,
        )
        if found is None:
            found = c.create_folder(part, parent_id).id
            refresh(c)
        parent_id = found
    return parent_id


def ink_token(doc) -> Optional[str]:
    """Fingerprint of a document's stroke files, from metadata alone.

    Changes when pen strokes change, but not when the tablet merely updates
    metadata (last opened page, zoom). Falls back to the document hash for
    transports without per-file hashes.
    """
    files = getattr(doc, "files", None) or []
    rm = sorted(
        (f.get("id", ""), f.get("hash", "")) for f in files if str(f.get("id", "")).endswith(".rm")
    )
    if files:
        h = hashlib.sha1()
        for fid, fhash in rm:
            h.update(f"{fid}:{fhash};".encode())
        return h.hexdigest()[:16]
    return getattr(doc, "hash", None)


def upload_pdf(pdf: bytes, name: str, folder: str, orientation: str = "portrait"):
    """Upload a generated PDF into ``folder`` (created if missing); returns the document."""
    c = client()
    parent_id = ensure_folder(c, folder)
    doc = c.upload_document(pdf, name, "pdf", parent_id, orientation=orientation)
    refresh(c)
    return doc


def download_zip(c, doc) -> bytes:
    data = c.download(doc)
    if not data:
        raise RuntimeError("The transport returned an empty document payload.")
    if data[:5] == b"%PDF-":
        raise RuntimeError(
            "This transport returns flattened PDFs without stroke data; "
            "use cloud or SSH mode to analyse annotations."
        )
    return data


def doc_status(doc, baseline: Optional[str], by_id: dict) -> Tuple[str, Optional[str]]:
    """(status, location) of a tracked document.

    ``baseline`` is the ink token recorded when the document was sent or last
    read. status is one of:
    - "missing":   deleted or in the trash
    - "done":      moved to a done folder with ink not read yet
    - "collected": in a done folder and already read
    - "annotated": ink changed since the baseline
    - "waiting":   no new ink
    """
    if doc is None or is_trashed(doc, by_id):
        return "missing", None
    path = get_item_path(doc, by_id)
    changed = ink_token(doc) != baseline
    parent = by_id.get(getattr(doc, "Parent", "") or "")
    if parent is not None and parent.VissibleName.strip().lower() in DONE_FOLDER_NAMES:
        return ("done" if changed else "collected"), path
    return ("annotated" if changed else "waiting"), path


def with_images(payload: str, images: Sequence[tuple]) -> list:
    """JSON payload followed by labelled PNG images, as MCP content blocks."""
    blocks: list = [TextContent(type="text", text=payload)]
    for label, what, png in images:
        blocks.append(TextContent(type="text", text=f"{label}: {what}"))
        blocks.append(
            ImageContent(type="image", data=base64.b64encode(png).decode(), mimeType="image/png")
        )
    return blocks
