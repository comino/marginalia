"""MCP tools for the Agent Inbox: handwritten requests -> agent tasks."""

from __future__ import annotations

import asyncio
import re
from datetime import datetime
from typing import Dict, List, Optional

from mcp.types import ToolAnnotations

from remarkable_mcp.api import get_items_by_id
from remarkable_mcp.responses import make_error, make_response
from remarkable_mcp.workflows import cloud, handwriting
from remarkable_mcp.workflows.inbox import (
    match_known,
    render_inbox_template,
    replies_markdown,
    segment_entries,
)
from remarkable_mcp.workflows.ink import load_document_ink_from_zip
from remarkable_mcp.workflows.state import Store, now_iso

DEFAULT_FOLDER = "/Agent"
REPLIES_FOLDER = "/Agent/Replies"
_TAG = re.compile(r"#([\wäöüÄÖÜß-]+)")

_WRITE = ToolAnnotations(
    title="Agent Inbox (write)",
    read_only_hint=False,
    destructive_hint=False,
    idempotent_hint=False,
    open_world_hint=False,
)
_READ = ToolAnnotations(
    title="Read Agent Inbox",
    read_only_hint=False,  # records entries in local state
    destructive_hint=False,
    idempotent_hint=True,
    open_world_hint=False,
)


def _store() -> Store:
    return Store("inbox")


async def remarkable_inbox_setup(
    document: Optional[str] = None,
    folder: str = DEFAULT_FOLDER,
    pages: int = 12,
    name: str = "default",
) -> str:
    """
    <usecase>Create (or register) the Agent Inbox the user writes requests into.</usecase>
    <instructions>
    Without `document`: uploads a ruled "Agent Inbox" PDF to `folder`.
    With `document`: uses an existing notebook or PDF of that name instead
    (nothing is uploaded).
    The user writes one request per block, leaves an empty line between
    requests, and strikes a request through to cancel it. #tags are extracted.
    Then poll with remarkable_inbox().
    </instructions>
    <parameters>
    - document: Existing document to use as the inbox (name or path). Optional.
    - folder: Where to upload the generated inbox (default "/Agent").
    - pages: Pages in the generated inbox (default 12; the user can add more).
    - name: Inbox name, for several inboxes (default "default").
    </parameters>
    """

    def work():
        c = cloud.client()
        if document:
            from remarkable_mcp.tools import _find_target_document

            items = c.get_meta_items()
            doc = _find_target_document(items, get_items_by_id(items), document)
            if doc is None:
                raise LookupError(document)
            return doc, False
        if not cloud.is_cloud():
            raise RuntimeError("Uploading the inbox needs cloud mode; pass document= instead.")
        with cloud.MUPDF_LOCK:
            pdf = render_inbox_template(max(1, min(pages, 60)))
        title = "Agent Inbox" if name == "default" else f"Agent Inbox · {name}"
        return cloud.upload_pdf(pdf, title, folder), True

    try:
        doc, uploaded = await asyncio.to_thread(work)
    except LookupError:
        return make_error(
            "document_not_found", f"Document not found: '{document}'", "Use remarkable_browse()."
        )
    except Exception as exc:
        return make_error("setup_failed", str(exc), "Check remarkable_status().")

    def put(current):
        current = current or {"name": name, "entries": {}, "created_at": now_iso()}
        current.update({"doc_id": doc.ID, "document": doc.VissibleName})
        return current

    _store().update(name, put)
    return make_response(
        {"inbox": name, "document": doc.VissibleName, "uploaded": uploaded},
        "Ready. Ask the user to write requests into it, then call remarkable_inbox().",
    )


def _scan(record: dict, include_images: bool):
    c = cloud.client()
    cloud.refresh(c)
    doc = cloud.find_by_id(c, record["doc_id"])
    if doc is None:
        raise LookupError(record.get("document", "inbox"))
    zip_bytes = cloud.download_zip(c, doc)
    with cloud.MUPDF_LOCK:
        ink = load_document_ink_from_zip(zip_bytes)
    found = []
    for page in ink.annotated_pages():
        found.extend(segment_entries(page))
    return doc, found


async def remarkable_inbox(
    pending_only: bool = True,
    include_images: bool = False,
    name: str = "default",
):
    """
    <usecase>Get the handwritten requests from the Agent Inbox.</usecase>
    <instructions>
    Each block of handwriting is one entry with a stable id, the page, its
    transcription (when a handwriting backend is configured) and #tags.
    status: "pending" (new or edited, needs action), "done" (you acknowledged it
    with remarkable_inbox_done), "cancelled" (the user struck it through).
    An entry the user extends after you marked it done becomes pending again.
    Set include_images=true to read entries yourself when "text" is null.
    </instructions>
    <parameters>
    - pending_only: Only entries that need action (default true).
    - include_images: Attach a crop per returned entry.
    - name: Inbox name (default "default").
    </parameters>
    """
    record = _store().get(name)
    if record is None:
        return make_error(
            "inbox_not_set_up", f"No inbox '{name}'.", "Call remarkable_inbox_setup() first."
        )
    try:
        doc, found = await asyncio.to_thread(_scan, record, include_images)
    except LookupError:
        return make_error(
            "document_missing",
            "The inbox document is no longer on the tablet.",
            "Run remarkable_inbox_setup() again.",
        )
    except Exception as exc:
        return make_error("inbox_failed", str(exc), "Check remarkable_status().")

    known: Dict[str, dict] = {k: dict(v) for k, v in record.get("entries", {}).items()}
    now = now_iso()
    current: List[tuple] = []  # (stored entry dict, Entry)
    reopened = set()  # ids that are new or grew in this scan
    cancelled = set()
    for e in found:
        old = match_known(e, known.values())
        if old is None:
            stored = {
                "id": e.id,
                "first_seen": now,
                "status": "pending",
                "fingerprints": sorted(e.fingerprints),
            }
            known[e.id] = stored
            reopened.add(e.id)
        else:
            stored = old
            grew = not set(e.fingerprints) <= set(old.get("fingerprints", []))
            if grew:
                stored["fingerprints"] = sorted(set(old["fingerprints"]) | e.fingerprints)
                stored["updated_at"] = now
                stored["text"] = None  # re-transcribe
                stored["status"] = "pending"
                reopened.add(stored["id"])
        if e.cancelled and stored["status"] != "done":
            stored["status"] = "cancelled"
            cancelled.add(stored["id"])
        elif not e.cancelled and stored["status"] == "cancelled":
            stored["status"] = "pending"  # the strike-through was erased
            reopened.add(stored["id"])
        stored["page"] = e.page
        current.append((stored, e))

    wanted = [(s, e) for s, e in current if not pending_only or s["status"] == "pending"]

    def transcribe():
        crops = {s["id"]: handwriting.render_strokes_png(e.strokes, e.rect) for s, e in wanted}
        ink_of = {s["id"]: e.strokes for s, e in wanted}
        todo = [sid for sid in crops if not known[sid].get("text")]
        texts = handwriting.transcribe_many(
            [crops[sid] for sid in todo],
            handwriting.backend(),
            strokes=[ink_of[sid] for sid in todo],
        )
        return crops, todo, texts

    crops, todo, results = await asyncio.to_thread(transcribe)
    for sid, (text, _engine) in zip(todo, results):
        if text:
            known[sid]["text"] = text
            known[sid]["tags"] = sorted({t.lower() for t in _TAG.findall(text)})

    present = {s["id"] for s, _ in current}

    def merge(rec):
        if rec is None:
            return None
        entries = rec.setdefault("entries", {})
        for sid, stored in known.items():
            if sid not in entries or sid in reopened:
                entries[sid] = stored
                continue
            # Keep a status set meanwhile (e.g. done); refresh scan-derived fields.
            live = entries[sid]
            for key in ("fingerprints", "page", "text", "tags", "updated_at"):
                if key in stored:
                    live[key] = stored[key]
            if sid in cancelled and live.get("status") != "done":
                live["status"] = "cancelled"
        rec["last_scan"] = {"at": now, "ink": cloud.ink_token(doc)}
        return rec

    _store().update(name, merge)

    out = []
    images = []
    for s, e in wanted:
        stored = known[s["id"]]
        item = {
            "id": stored["id"],
            "status": stored["status"],
            "page": stored["page"],
            "text": stored.get("text"),
            "tags": stored.get("tags", []),
            "first_seen": stored["first_seen"],
        }
        if stored.get("updated_at"):
            item["updated_at"] = stored["updated_at"]
        out.append(item)
        if include_images:
            images.append((stored["id"], "inbox entry", crops[stored["id"]]))
    missing_text = sum(1 for i in out if i["text"] is None)
    hint = (
        f"{len(out)} entr{'y' if len(out) == 1 else 'ies'}. Act on pending entries, then call "
        "remarkable_inbox_done(entries=[...], replies={id: 'answer'}) to acknowledge them."
    )
    if missing_text and not include_images:
        hint += f" {missing_text} not transcribed: call with include_images=true to read them."
    if not out:
        hint = "Inbox is clear." if pending_only else "The inbox is empty."
    payload = make_response(
        {
            "inbox": name,
            "document": record.get("document"),
            "gone_from_tablet": sorted(set(known) - present) if not pending_only else None,
            "entries": out,
        },
        hint,
    )
    return cloud.with_images(payload, images) if images else payload


async def remarkable_inbox_done(
    entries: List[str],
    replies: Optional[Dict[str, str]] = None,
    send_replies: bool = True,
    name: str = "default",
) -> str:
    """
    <usecase>Acknowledge inbox entries and optionally answer them on the tablet.</usecase>
    <instructions>
    Marks the entries done so remarkable_inbox() stops returning them. With
    `replies` ({entry id: text}) and send_replies=true, one "Replies" PDF is
    uploaded to /Agent/Replies quoting each request with its answer - the
    user reads your answers on the tablet.
    </instructions>
    <parameters>
    - entries: Entry ids to mark done.
    - replies: Optional answers per entry id (Markdown allowed).
    - send_replies: Upload the replies as a PDF (default true).
    - name: Inbox name (default "default").
    </parameters>
    """
    replies = replies or {}
    ids = list(dict.fromkeys(list(entries) + list(replies)))
    unknown: List[str] = []
    answered: List[dict] = []

    def mark(rec):
        if rec is None:
            return None
        for eid in ids:
            e = rec.get("entries", {}).get(eid)
            if e is None:
                unknown.append(eid)
                continue
            e["status"] = "done"
            e["done_at"] = now_iso()
            if eid in replies:
                e["reply"] = replies[eid]
                answered.append({"id": eid, "request": e.get("text") or "", "reply": replies[eid]})
        return rec

    if _store().update(name, mark) is None:
        return make_error(
            "inbox_not_set_up", f"No inbox '{name}'.", "Call remarkable_inbox_setup()."
        )

    uploaded = None
    from remarkable_mcp.write_tools import write_enabled

    if answered and send_replies and not write_enabled():
        send_replies = False
    if answered and send_replies:
        if not cloud.is_cloud():
            return make_error(
                "unsupported_transport",
                "Entries were marked done, but replies can only be uploaded in cloud mode.",
                "Pass send_replies=false.",
            )
        from remarkable_mcp.markdown_pdf import render_markdown_pdf

        title = f"Replies {datetime.now().strftime('%d %b %H:%M')}"

        def upload():
            with cloud.MUPDF_LOCK:
                pdf = render_markdown_pdf(replies_markdown(answered))
            return cloud.upload_pdf(pdf, title, REPLIES_FOLDER)

        try:
            await asyncio.to_thread(upload)
            uploaded = f"{REPLIES_FOLDER}/{title}"
        except Exception as exc:
            return make_error(
                "reply_upload_failed",
                f"Entries marked done, but the replies upload failed: {exc}",
                "Retry with the same replies; marking done again is harmless.",
            )
    return make_response(
        {
            "done": [i for i in ids if i not in unknown],
            "unknown": unknown,
            "replies_document": uploaded,
        },
        "Acknowledged." + (f" Replies are on the tablet at {uploaded}." if uploaded else ""),
    )


def register(mcp, write_enabled: bool) -> None:
    mcp.tool(annotations=_READ)(remarkable_inbox)
    mcp.tool(annotations=_READ)(remarkable_inbox_done)
    if write_enabled:
        mcp.tool(annotations=_WRITE)(remarkable_inbox_setup)
