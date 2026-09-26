"""Transport glue shared by the workflow tools.

Tool modules call these through the module (``cloud.client()``), never via
``from ... import``, so tests can swap the whole transport in one place.
"""

from __future__ import annotations

import base64
import logging
from typing import Optional, Sequence, Tuple

from mcp.types import ImageContent, TextContent

from remarkable_mcp.api import get_item_path, get_items_by_id, get_rmapi

logger = logging.getLogger(__name__)

# A document counts as "done" once it sits in a folder with one of these names.
DONE_FOLDER_NAMES = {"reviewed", "done", "erledigt", "answered", "beantwortet"}


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


def find_by_id(c, doc_id: str):
    return next((d for d in c.get_meta_items() if d.ID == doc_id), None)


def ensure_folder(c, path: str) -> str:
    """Resolve ``/A/B`` to a folder id, creating missing levels."""
    from remarkable_mcp.write_tools import _resolve_parent_id

    parent_id = ""
    walked = ""
    for part in [p for p in path.strip("/").split("/") if p]:
        walked += "/" + part
        collection = c.get_meta_items()
        found = _resolve_parent_id(walked, get_items_by_id(collection), collection)
        if found is None:
            found = c.create_folder(part, parent_id).id
            refresh(c)
        parent_id = found
    return parent_id


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


def doc_status(doc, baseline_hash: Optional[str], by_id: dict) -> Tuple[str, Optional[str]]:
    """(status, location) of a tracked document.

    status: "missing" | "done" (moved to a done folder) | "annotated" (changed
    since ``baseline_hash``) | "waiting".
    """
    if doc is None:
        return "missing", None
    path = get_item_path(doc, by_id)
    parent = by_id.get(getattr(doc, "Parent", "") or "")
    if parent is not None and parent.VissibleName.strip().lower() in DONE_FOLDER_NAMES:
        return "done", path
    current = getattr(doc, "hash", None)
    if current and baseline_hash and current != baseline_hash:
        return "annotated", path
    return "waiting", path


def with_images(payload: str, images: Sequence[tuple]) -> list:
    """JSON payload followed by labelled PNG images, as MCP content blocks."""
    blocks: list = [TextContent(type="text", text=payload)]
    for label, what, png in images:
        blocks.append(TextContent(type="text", text=f"{label}: {what}"))
        blocks.append(
            ImageContent(type="image", data=base64.b64encode(png).decode(), mimeType="image/png")
        )
    return blocks
