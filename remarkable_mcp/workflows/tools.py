"""MCP tools for workflows: review round-trips and ink analysis.

These tools do the heavy lifting server-side (layout, geometry, anchoring,
state) and answer with compact JSON, so a small agent can run a full review
loop with three calls: send -> collect -> send (next version).
"""

from __future__ import annotations

import asyncio
import logging
import os
from pathlib import Path
from typing import List, Optional

from mcp.types import ToolAnnotations

from remarkable_mcp.api import get_items_by_id
from remarkable_mcp.responses import make_error, make_response
from remarkable_mcp.server import mcp
from remarkable_mcp.workflows import cloud, handwriting
from remarkable_mcp.workflows.ink import load_document_ink_from_zip
from remarkable_mcp.workflows.review import collect_requests, source_digest
from remarkable_mcp.workflows.review_pdf import render_review_pdf
from remarkable_mcp.workflows.state import Store, now_iso, slugify

logger = logging.getLogger(__name__)

DEFAULT_REVIEW_FOLDER = os.environ.get("REMARKABLE_REVIEW_FOLDER", "/Review")

_READ = ToolAnnotations(
    title="Analyse reMarkable Annotations",
    read_only_hint=True,
    destructive_hint=False,
    idempotent_hint=True,
    open_world_hint=False,
)
_SEND = ToolAnnotations(
    title="Send Draft for Review",
    read_only_hint=False,
    destructive_hint=False,
    idempotent_hint=False,
    open_world_hint=False,
)
_COLLECT = ToolAnnotations(
    title="Collect Review Feedback",
    read_only_hint=False,  # records which marks were seen (local state only)
    destructive_hint=False,
    idempotent_hint=False,
    open_world_hint=False,
)
_LIST = ToolAnnotations(
    title="List Reviews",
    read_only_hint=True,
    destructive_hint=False,
    idempotent_hint=True,
    open_world_hint=False,
)


def _reviews() -> Store:
    return Store("reviews")


def _note_payloads(requests, ink, include_images: bool):
    """Transcribe notes; return ({mark id: (text, status)}, [image blocks])."""
    engine = handwriting.backend()
    notes = {}
    images: List[tuple] = []
    pages = {p.page: p for p in ink.pages}
    for req in requests:
        mark = req.mark
        note = mark if mark.kind == "note" else mark.note
        if note is None:
            if include_images:
                pg = pages[req.page]
                png = handwriting.render_page_region_png(
                    ink.pdf_bytes, pg.pdf_page, mark.strokes, mark.rect
                )
                images.append((mark.id, "mark in context", png))
            continue
        png = handwriting.render_strokes_png(note.strokes, note.rect)
        text, used = handwriting.transcribe(png, engine)
        if text is not None:
            notes[mark.id] = (text, f"transcribed:{used}")
        else:
            notes[mark.id] = (None, "image" if include_images else "not_transcribed")
        if include_images:
            images.append((mark.id, "handwritten note", png))
    return notes, images


def _shape(requests, notes) -> List[dict]:
    out = []
    for req in requests:
        text, status = notes.get(req.mark.id, (None, "none"))
        out.append(req.to_dict(text, status))
    return out


def _counts(requests) -> dict:
    by_intent: dict = {}
    for r in requests:
        by_intent[r["intent"]] = by_intent.get(r["intent"], 0) + 1
    return by_intent


async def remarkable_review_send(
    source_path: Optional[str] = None,
    markdown: Optional[str] = None,
    title: Optional[str] = None,
    review: Optional[str] = None,
    folder: str = DEFAULT_REVIEW_FOLDER,
    legend: Optional[bool] = None,
    responses: Optional[List[dict]] = None,
) -> str:
    """
    <usecase>Send a Markdown draft (e.g. a blog article) to the tablet for pen review.</usecase>
    <instructions>
    Renders the draft as a review PDF: numbered paragraphs in the left margin,
    wide line spacing, a blank right margin for handwritten comments. The server
    remembers which paragraph came from which source lines, so
    remarkable_review_collect can later turn pen marks into precise change
    requests.

    First call for a draft creates version 1. Calling again with the same
    review (or the same title) sends the next version: changed paragraphs get
    a black bar in the left margin, and `responses` adds a closing page that
    answers the previous round's comments.

    Provide either source_path (a local .md file; YAML front matter is read for
    the title and skipped when rendering) or markdown text.
    Cloud mode only (uploads via the sync API).
    </instructions>
    <parameters>
    - source_path: Local Markdown file to review (preferred; enables exact line mapping).
    - markdown: Markdown text, when there is no file.
    - title: Display title (default: front matter title, else the file name).
    - review: Review id from an earlier send, to send the next version.
    - folder: Tablet folder to upload into (default "/Review"; created if missing).
    - legend: Prepend a one-page marking guide (default: only for version 1).
    - responses: For version 2+: [{"id": "<request id>", "status": "done|skipped|partial",
      "reply": "what changed"}] answering the previous collect's requests.
    </parameters>
    <examples>
    - remarkable_review_send(source_path="/home/me/blog/drafts/post.md")
    - remarkable_review_send(source_path="/home/me/blog/drafts/post.md", review="post",
        responses=[{"id": "m1a2b3c4", "status": "done", "reply": "Cut the paragraph"}])
    </examples>
    """
    if bool(source_path) == bool(markdown):
        return make_error(
            "invalid_arguments",
            "Provide exactly one of source_path or markdown.",
            "Pass source_path='/path/to/draft.md' for a file on disk.",
        )
    if source_path:
        path = Path(source_path).expanduser()
        if not path.is_file():
            return make_error(
                "file_not_found", f"No such file: {source_path}", "Pass an absolute path."
            )
        text = path.read_text()
        source_path = str(path.resolve())
    else:
        text = markdown or ""

    store = _reviews()
    record = None
    if review:
        record = store.get(slugify(review))
        if record is None:
            return make_error(
                "review_not_found",
                f"No review '{review}'.",
                "Use remarkable_review_list() to see known reviews, or omit review to start one.",
            )
    elif source_path:
        record = store.find(lambda r: r.get("source_path") == source_path)

    version = (record["versions"][-1]["version"] + 1) if record else 1
    previous = record["versions"][-1] if record else None
    legend = (version == 1) if legend is None else legend

    quoted = []
    if responses:
        last = {r["id"]: r for r in (record or {}).get("last_requests", [])}
        for r in responses:
            req = last.get(r.get("id"), {})
            quote = req.get("note") or req.get("target") or ""
            label = r.get("id", "")
            if req.get("paragraph"):
                label = f"¶{req['paragraph']} {req.get('intent', '')}".strip()
            quoted.append(
                {
                    "id": label,
                    "status": r.get("status", ""),
                    "reply": (f"“{quote[:80]}” → " if quote else "") + r.get("reply", ""),
                }
            )

    try:
        rendered = await asyncio.to_thread(
            render_review_pdf,
            text,
            title=title or (record or {}).get("title"),
            version=version,
            legend=legend,
            previous_digests=[b["digest"] for b in previous["blocks"]] if previous else None,
            responses=quoted or None,
        )
    except Exception as exc:
        return make_error(
            "render_failed", f"Could not render the draft: {exc}", "Check the Markdown."
        )

    if (
        not title
        and not (record or {}).get("title")
        and source_path
        and not rendered.front_matter.get("title")
    ):
        rendered.title = Path(source_path).stem.replace("-", " ").strip() or rendered.title
    slug = (
        record["slug"]
        if record
        else slugify(review or rendered.front_matter.get("slug") or rendered.title)
    )
    if record is None and store.get(slug) is not None:
        slug = f"{slug}-{source_digest(text)[:4]}"

    if not cloud.is_cloud():
        return make_error(
            "unsupported_transport",
            "remarkable_review_send currently uploads through the cloud sync API only.",
            "Run the server in cloud mode (the default).",
        )

    doc_name = f"{rendered.title} · v{version}"

    def upload():
        return cloud.upload_pdf(rendered.pdf, doc_name, folder)

    try:
        doc = await asyncio.to_thread(upload)
    except Exception as exc:
        return make_error("upload_failed", f"Upload failed: {exc}", "Check remarkable_status().")

    entry = {
        "version": version,
        "doc_id": doc.id,
        "doc_name": doc_name,
        "folder": folder,
        "sent_at": now_iso(),
        "page_count": rendered.page_count,
        "layout": rendered.layout,
        "blocks": rendered.manifest_blocks(),
        "source_text": text,
        "source_sha": source_digest(text),
        "doc_hash_at_send": getattr(doc, "hash", None),
    }
    record = record or {
        "slug": slug,
        "title": rendered.title,
        "source_path": source_path,
        "created_at": now_iso(),
        "versions": [],
        "seen": [],
    }
    record["versions"].append(entry)
    store.put(record["slug"], record)

    changed = [b.number for b in rendered.blocks if b.changed]
    return make_response(
        {
            "review": record["slug"],
            "version": version,
            "document": doc_name,
            "folder": folder,
            "pages": rendered.page_count,
            "paragraphs": len(rendered.blocks),
            "changed_paragraphs": changed if previous else None,
        },
        f"Sent to the tablet as '{doc_name}' in {folder}. After the reviewer has marked it up, "
        f"call remarkable_review_collect('{record['slug']}').",
    )


async def remarkable_review_collect(
    review: str,
    version: Optional[int] = None,
    only_new: bool = True,
    include_images: bool = False,
    mark_seen: bool = True,
):
    """
    <usecase>Turn the pen marks on a reviewed draft into a list of change requests.</usecase>
    <instructions>
    Downloads the review document and interprets the ink:
    - strikethrough / scribble  -> intent "delete" (with a nearby note: "replace")
    - underline / circle / highlight / margin bar -> "attention" (with a note: "change")
    - handwriting on its own -> "comment" on the paragraph beside it
    Each request names the paragraph number, its source line range, the exact
    source line of the marked text when it can be found, the marked text, and
    the handwritten note (transcribed when a handwriting backend is configured).

    By default only marks not returned before are included, so you can collect
    after every reading session. Set include_images=true to also get cropped
    images of notes (and of marks) - use this when note_status is
    "not_transcribed" or a transcription looks wrong.
    </instructions>
    <parameters>
    - review: Review id (from remarkable_review_send / remarkable_review_list).
    - version: Which version's document to read (default: latest).
    - only_new: Only return marks not collected before (default true).
    - include_images: Attach PNG crops of handwriting and marks (default false).
    - mark_seen: Remember returned marks so the next collect skips them (default true).
    </parameters>
    <examples>
    - remarkable_review_collect("my-post")
    - remarkable_review_collect("my-post", include_images=True)
    - remarkable_review_collect("my-post", only_new=False, mark_seen=False)  # everything, again
    </examples>
    """
    store = _reviews()
    record = store.get(slugify(review))
    if record is None:
        return make_error(
            "review_not_found", f"No review '{review}'.", "Use remarkable_review_list()."
        )
    versions = record["versions"]
    entry = (
        next((v for v in versions if v["version"] == version), None) if version else versions[-1]
    )
    if entry is None:
        return make_error(
            "version_not_found",
            f"Review '{review}' has no version {version}.",
            f"Known versions: {[v['version'] for v in versions]}",
        )

    def work():
        client = cloud.client()
        cloud.refresh(client)
        doc = cloud.find_by_id(client, entry["doc_id"])
        if doc is None:
            raise LookupError(entry["doc_name"])
        zip_bytes = cloud.download_zip(client, doc)
        ink = load_document_ink_from_zip(zip_bytes)
        seen = set(record.get("seen", []))
        reqs = collect_requests(ink, entry["blocks"], entry.get("source_text"), seen)
        if only_new:
            reqs = [r for r in reqs if r.new]
        notes, images = _note_payloads(reqs, ink, include_images)
        return doc, reqs, notes, images

    try:
        doc, reqs, notes, images = await asyncio.to_thread(work)
    except LookupError:
        return make_error(
            "document_missing",
            f"The review document '{entry['doc_name']}' is no longer on the tablet.",
            "It may have been deleted. Send a new version with remarkable_review_send.",
        )
    except Exception as exc:
        return make_error("collect_failed", str(exc), "Check remarkable_status().")

    shaped = _shape(reqs, notes)
    if mark_seen and reqs:
        seen = set(record.get("seen", []))
        seen.update(r.mark.id for r in reqs)
        record["seen"] = sorted(seen)
    if shaped:
        record["last_requests"] = shaped
    record["last_collect"] = {
        "version": entry["version"],
        "at": now_iso(),
        "doc_hash": doc.hash if hasattr(doc, "hash") else None,
    }
    store.put(record["slug"], record)

    untranscribed = sum(1 for r in shaped if r.get("note_status") == "not_transcribed")
    hint = (
        f"{len(shaped)} request(s). Apply them to {record.get('source_path') or 'the draft'} "
        "(src_line / src_lines point into that file), then call remarkable_review_send with "
        "review and responses=[{id, status, reply}] to send the next version."
    )
    if untranscribed:
        hint += (
            f" {untranscribed} handwritten note(s) are not transcribed: call again with "
            "include_images=true, only_new=false, mark_seen=false to read them."
        )
    if not shaped:
        hint = "No new marks. The reviewer may not have synced yet; try again later."
    payload = make_response(
        {
            "review": record["slug"],
            "title": record["title"],
            "version": entry["version"],
            "source_path": record.get("source_path"),
            "handwriting_backend": handwriting.backend(),
            "counts": _counts(shaped),
            "requests": shaped,
        },
        hint,
    )
    return cloud.with_images(payload, images) if images else payload


async def remarkable_review_list() -> str:
    """
    <usecase>List review rounds and which ones have new pen marks waiting.</usecase>
    <instructions>
    Shows every draft sent with remarkable_review_send: its versions, where the
    latest document sits on the tablet, and a status:
    - "done": the reviewer moved it to a folder named Reviewed / Done
    - "annotated": the document changed since it was sent or last collected
    - "waiting": untouched since it was sent or collected
    - "missing": the document is gone from the tablet
    Use it to decide which review to collect.
    </instructions>
    """
    records = list(_reviews().all())
    if not records:
        return make_response(
            {"reviews": []}, "No reviews yet. Start one with remarkable_review_send."
        )

    def work():
        client = cloud.client()
        cloud.refresh(client)
        collection = client.get_meta_items()
        by_id = get_items_by_id(collection)
        rows = []
        for r in records:
            latest = r["versions"][-1]
            doc = by_id.get(latest["doc_id"])
            row = {
                "review": r["slug"],
                "title": r["title"],
                "versions": len(r["versions"]),
                "document": latest["doc_name"],
                "source_path": r.get("source_path"),
                "last_collect": (r.get("last_collect") or {}).get("at"),
            }
            baseline = (r.get("last_collect") or {}).get("doc_hash") or latest.get(
                "doc_hash_at_send"
            )
            row["status"], row["location"] = cloud.doc_status(doc, baseline, by_id)
            rows.append(row)
        return rows

    try:
        rows = await asyncio.to_thread(work)
    except Exception as exc:
        return make_error("list_failed", str(exc), "Check remarkable_status().")
    ready = [r["review"] for r in rows if r.get("status") in ("done", "annotated")]
    return make_response(
        {"reviews": rows},
        f"Ready to collect: {', '.join(ready)}." if ready else "Nothing new to collect.",
    )


async def remarkable_annotations(
    document: str,
    pages: Optional[List[int]] = None,
    include_images: bool = False,
):
    """
    <usecase>Interpret the pen marks on any annotated PDF or notebook.</usecase>
    <instructions>
    Classifies the ink on each page and anchors it to the printed text:
    strikethroughs, scribble-outs, underlines, circles, highlights, margin bars
    and handwritten notes, each with the words it marks and the text region
    it belongs to. Handwritten notes are transcribed when a handwriting backend
    is configured; otherwise set include_images=true to receive crops.

    Use remarkable_review_collect instead for drafts sent with
    remarkable_review_send - it also maps marks to source lines.
    </instructions>
    <parameters>
    - document: Document name or path.
    - pages: 1-based page numbers to analyse (default: all annotated pages).
    - include_images: Attach PNG crops of notes and marks (default false).
    </parameters>
    <examples>
    - remarkable_annotations("Standup 24 Sep")
    - remarkable_annotations("/Work/Contract draft", pages=[2, 3], include_images=True)
    </examples>
    """
    from remarkable_mcp.tools import _find_target_document

    def work():
        client = cloud.client()
        collection = client.get_meta_items()
        doc = _find_target_document(collection, get_items_by_id(collection), document)
        if doc is None:
            raise LookupError(document)
        ink = load_document_ink_from_zip(cloud.download_zip(client, doc), pages)
        reqs = collect_requests(ink)
        notes, images = _note_payloads(reqs, ink, include_images)
        return doc, ink, reqs, notes, images

    try:
        doc, ink, reqs, notes, images = await asyncio.to_thread(work)
    except LookupError:
        return make_error(
            "document_not_found",
            f"Document not found: '{document}'",
            "Use remarkable_browse(query=...) to find the exact name.",
        )
    except Exception as exc:
        return make_error("analysis_failed", str(exc), "Check remarkable_status().")

    shaped = _shape(reqs, notes)
    for item in shaped:
        item.pop("seen_before", None)
    payload = make_response(
        {
            "document": doc.VissibleName,
            "pages_with_ink": [p.page for p in ink.annotated_pages()],
            "handwriting_backend": handwriting.backend(),
            "counts": _counts(shaped),
            "marks": shaped,
        },
        f"{len(shaped)} mark(s) found."
        + (
            " Notes are not transcribed; call with include_images=true to read them."
            if any(m.get("note_status") == "not_transcribed" for m in shaped)
            else ""
        ),
    )
    return cloud.with_images(payload, images) if images else payload


def register_workflow_tools(write_enabled: bool) -> None:
    """Register workflow tools; sending needs write mode, analysis does not."""
    mcp.tool(annotations=_READ)(remarkable_annotations)
    mcp.tool(annotations=_LIST)(remarkable_review_list)
    mcp.tool(annotations=_COLLECT)(remarkable_review_collect)
    if write_enabled:
        mcp.tool(annotations=_SEND)(remarkable_review_send)

    from remarkable_mcp.workflows import form_tools

    form_tools.register(mcp, write_enabled)


def _register_on_import() -> None:
    from remarkable_mcp.write_tools import write_enabled

    register_workflow_tools(write_enabled())


_register_on_import()
