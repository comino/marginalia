"""MCP tools for paper forms and quick questions answered with the pen."""

from __future__ import annotations

import asyncio
import uuid
from typing import List, Optional

from mcp.types import ToolAnnotations

from remarkable_mcp.api import get_items_by_id
from remarkable_mcp.responses import make_error, make_response
from remarkable_mcp.workflows import cloud, handwriting
from remarkable_mcp.workflows.forms import (
    FormSpecError,
    read_answers,
    render_form,
    stray_strokes,
)
from remarkable_mcp.workflows.ink import load_document_ink_from_zip
from remarkable_mcp.workflows.marks import _cluster, _union
from remarkable_mcp.workflows.state import Store, now_iso, slugify

DEFAULT_FORMS_FOLDER = "/Agent/Forms"

_SEND = ToolAnnotations(
    title="Send Form to Tablet",
    read_only_hint=False,
    destructive_hint=False,
    idempotent_hint=False,
    open_world_hint=False,
)
_READ = ToolAnnotations(
    title="Read Form Answers",
    read_only_hint=False,  # stores the answers in local state
    destructive_hint=False,
    idempotent_hint=True,
    open_world_hint=False,
)
_LIST = ToolAnnotations(
    title="List Forms",
    read_only_hint=True,
    destructive_hint=False,
    idempotent_hint=True,
    open_world_hint=False,
)


def _forms() -> Store:
    return Store("forms")


def _send(title: str, fields: list, intro: str, folder: str, kind: str) -> str:
    try:
        with cloud.MUPDF_LOCK:
            rendered = render_form(title, fields, intro=intro)
    except FormSpecError as exc:
        return make_error("invalid_form", str(exc), "Fix the field list and try again.")
    if not cloud.is_cloud():
        return make_error(
            "unsupported_transport",
            "Forms are uploaded through the cloud sync API only.",
            "Run the server in cloud mode (the default).",
        )
    form_id = f"{slugify(title, 40)}-{uuid.uuid4().hex[:6]}"
    doc = cloud.upload_pdf(rendered.pdf, title, folder)
    record = {
        "id": form_id,
        "kind": kind,
        "title": title,
        "doc_id": doc.id,
        "folder": folder,
        "sent_at": now_iso(),
        "ink_at_send": cloud.ink_token(doc),
        "manifest": rendered.manifest(),
    }
    _forms().put(form_id, record)
    return make_response(
        {
            "form": form_id,
            "document": title,
            "folder": folder,
            "pages": rendered.page_count,
            "fields": [f["id"] for f in rendered.fields if "id" in f],
        },
        f"On the tablet in {folder}. Read the answers later with "
        f"remarkable_form_read('{form_id}'); remarkable_form_list() shows which forms changed.",
    )


async def remarkable_form_send(
    title: str,
    fields: List[dict],
    intro: str = "",
    folder: str = DEFAULT_FORMS_FOLDER,
) -> str:
    """
    <usecase>Put a form on the tablet that the user fills in with the pen.</usecase>
    <instructions>
    Renders the fields as a PDF with boxes. Reading the answers back does not
    depend on handwriting recognition for choices: a tick, cross or circle in
    or around a box selects it, and filling a box solid undoes the tick.

    Field types (id is optional, defaults to q1, q2, ...):
    - {"id", "type": "checkbox", "label"}                  -> true/false
    - {"id", "type": "choice", "label", "options": [..]}   -> one option
    - {"id", "type": "multi",  "label", "options": [..]}   -> list of options
    - {"id", "type": "scale",  "label", "min": 1, "max": 5, "min_label", "max_label"} -> int
    - {"id", "type": "text",   "label", "lines": 2}        -> handwriting
    - {"type": "heading", "label"} and {"type": "info", "label"} for layout
    Use remarkable_ask for a single question.
    </instructions>
    <parameters>
    - title: Form title (also the document name on the tablet).
    - fields: List of field objects as above.
    - intro: Optional instructions shown under the title.
    - folder: Tablet folder (default "/Agent/Forms", created if missing).
    </parameters>
    <examples>
    - remarkable_form_send("Weekly check-in", [
        {"id": "energy", "type": "scale", "label": "Energy this week", "min": 1, "max": 5},
        {"id": "blockers", "type": "text", "label": "Blockers", "lines": 3}])
    </examples>
    """
    try:
        return await asyncio.to_thread(_send, title, fields, intro, folder, "form")
    except Exception as exc:
        return make_error("send_failed", str(exc), "Check remarkable_status().")


async def remarkable_ask(
    question: str,
    options: Optional[List[str]] = None,
    context: str = "",
    allow_comment: bool = True,
    multiple: bool = False,
    folder: str = DEFAULT_FORMS_FOLDER,
) -> str:
    """
    <usecase>Ask the user one question on the tablet and get a pen answer later.</usecase>
    <instructions>
    A human-in-the-loop decision: creates a one-page form with the question,
    the options as boxes (default Yes / No) and an optional comment area. The
    user ticks or circles an option. Read the answer with
    remarkable_form_read(form) - it returns "answered": false until then.
    Keep options short. Put background the user needs into context.
    </instructions>
    <parameters>
    - question: The question.
    - options: Choices (default ["Yes", "No"]).
    - context: Background shown above the question.
    - allow_comment: Add a free-text comment area (default true).
    - multiple: Allow several options (default false).
    - folder: Tablet folder (default "/Agent/Forms").
    </parameters>
    <examples>
    - remarkable_ask("Publish the DuckDB post on Tuesday?")
    - remarkable_ask("Which title?", ["A Disposable DuckDB Workspace", "SQL Without a Shell"],
        context="Both fit the SEO description.")
    </examples>
    """
    opts = options or ["Yes", "No"]
    fields: list = []
    if context:
        fields.append({"type": "info", "label": context})
    fields.append(
        {
            "id": "answer",
            "type": "multi" if multiple else "choice",
            "label": question,
            "options": opts,
        }
    )
    if allow_comment:
        fields.append({"id": "comment", "type": "text", "label": "Comment (optional)", "lines": 3})
    title = f"Question: {question[:60]}"
    try:
        return await asyncio.to_thread(_send, title, fields, "", folder, "ask")
    except Exception as exc:
        return make_error("send_failed", str(exc), "Check remarkable_status().")


def _read(record: dict, include_images: bool):
    c = cloud.client()
    cloud.refresh(c)
    doc = cloud.find_by_id(c, record["doc_id"])
    if doc is None:
        raise LookupError(record["title"])
    zip_bytes = cloud.download_zip(c, doc)
    manifest = record["manifest"]
    with cloud.MUPDF_LOCK:
        ink = load_document_ink_from_zip(zip_bytes)
    pages = {p.pdf_page + 1: p for p in ink.pages if p.pdf_page is not None}
    answers = read_answers(manifest, pages)
    engine = handwriting.backend()
    images = []
    out_fields = []
    values = {}
    crops = {}  # field id / remark id -> png
    for ans in answers:
        if ans.field["type"] == "text" and ans.strokes:
            crops[ans.field["id"]] = handwriting.render_strokes_png(ans.strokes, ans.rect)
    remarks = []
    for pno, strokes in stray_strokes(manifest, pages).items():
        for g in _cluster([s.bbox for s in strokes], 12, 8):
            group = [strokes[i] for i in g]
            label = f"remark-p{pno}-{len(remarks) + 1}"
            crops[label] = handwriting.render_strokes_png(group, _union([s.bbox for s in group]))
            remarks.append({"id": label, "page": pno, "text": None})
    labels = list(crops)
    texts = dict(zip(labels, handwriting.transcribe_many([crops[k] for k in labels], engine)))

    for ans in answers:
        f = ans.field
        item = {"id": f["id"], "type": f["type"], "label": f["label"], "status": ans.status}
        if f["type"] == "text":
            value = None
            if f["id"] in crops:
                value, used = texts[f["id"]]
                item["status"] = "answered" if value else "needs_transcription"
                if value:
                    item["engine"] = used
                if include_images:
                    images.append((f["id"], "handwritten answer", crops[f["id"]]))
            item["value"] = value
        else:
            item["value"] = ans.value
        item.update(ans.detail)
        values[f["id"]] = item["value"]
        out_fields.append(item)
    for remark in remarks:
        remark["text"] = texts[remark["id"]][0]
        if include_images:
            images.append((remark["id"], "margin remark", crops[remark["id"]]))
    return doc, out_fields, values, remarks, images


async def remarkable_form_read(form: str, include_images: bool = False):
    """
    <usecase>Read the answers from a form or question on the tablet.</usecase>
    <instructions>
    Returns "values" ({field id: value}) plus per-field status:
    - answered / empty
    - ambiguous: several options marked in a single choice; value is the best
      guess and "candidates" lists all of them - ask again if it matters
    - needs_transcription: handwriting present but no handwriting backend
      configured; call with include_images=true and read the crop
    "answered" is true when every choice/scale/checkbox field has an answer.
    Ink outside the answer boxes is returned as "remarks".
    </instructions>
    <parameters>
    - form: Form id from remarkable_form_send / remarkable_ask / remarkable_form_list.
    - include_images: Attach crops of handwritten answers and remarks.
    </parameters>
    <examples>
    - remarkable_form_read("question-publish-the-duckdb-post-3f9a1c")
    </examples>
    """
    record = _forms().get(form) if form else None
    if record is None:
        return make_error("form_not_found", f"No form '{form}'.", "Use remarkable_form_list().")
    try:
        doc, fields, values, remarks, images = await asyncio.to_thread(
            _read, record, include_images
        )
    except LookupError:
        return make_error(
            "document_missing",
            f"The form '{record['title']}' is no longer on the tablet.",
            "Send it again if you still need the answers.",
        )
    except Exception as exc:
        return make_error("read_failed", str(exc), "Check remarkable_status().")

    choices = [f for f in fields if f["type"] in ("choice", "multi", "scale", "checkbox")]
    answered = bool(choices) and all(f["status"] in ("answered", "ambiguous") for f in choices)
    if record.get("kind") == "ask":
        answered = next((f["status"] != "empty" for f in fields if f["id"] == "answer"), False)

    def merge(current):
        if current is None:
            return None
        current["last_read"] = {"at": now_iso(), "ink": cloud.ink_token(doc)}
        current["answers"] = values
        return current

    _forms().update(record["id"], merge)

    hint = (
        "All questions answered." if answered else "Not (fully) answered yet - check again later."
    )
    if any(f["status"] == "ambiguous" for f in fields):
        hint += " Some choices are ambiguous (several options marked)."
    if any(f["status"] == "needs_transcription" for f in fields) and not include_images:
        hint += " Handwritten answers need include_images=true to be read."
    payload = make_response(
        {
            "form": record["id"],
            "title": record["title"],
            "answered": answered,
            "values": values,
            "fields": fields,
            "remarks": remarks,
        },
        hint,
    )
    return cloud.with_images(payload, images) if images else payload


async def remarkable_form_list(include_done: bool = True) -> str:
    """
    <usecase>List forms and questions sent to the tablet and whether they changed.</usecase>
    <instructions>
    status: "annotated" (ink added since sent / last read), "done" (moved to a
    folder named Done / Answered), "waiting", or "missing".
    </instructions>
    """
    records = list(_forms().all())

    def work():
        c = cloud.client()
        cloud.refresh(c)
        by_id = get_items_by_id(c.get_meta_items())
        rows = []
        for r in records:
            baseline = (r.get("last_read") or {}).get("ink") or r.get("ink_at_send")
            status, location = cloud.doc_status(by_id.get(r["doc_id"]), baseline, by_id)
            if status == "done" and not include_done:
                continue
            rows.append(
                {
                    "form": r["id"],
                    "kind": r.get("kind", "form"),
                    "title": r["title"],
                    "sent_at": r["sent_at"],
                    "status": status,
                    "location": location,
                    "last_answers": r.get("answers"),
                }
            )
        return rows

    if not records:
        return make_response(
            {"forms": []}, "No forms yet. Use remarkable_ask or remarkable_form_send."
        )
    try:
        rows = await asyncio.to_thread(work)
    except Exception as exc:
        return make_error("list_failed", str(exc), "Check remarkable_status().")
    changed = [r["form"] for r in rows if r["status"] in ("annotated", "done")]
    return make_response(
        {"forms": rows},
        f"Changed since last read: {', '.join(changed)}." if changed else "No new answers.",
    )


def register(mcp, write_enabled: bool) -> None:
    mcp.tool(annotations=_READ)(remarkable_form_read)
    mcp.tool(annotations=_LIST)(remarkable_form_list)
    if write_enabled:
        mcp.tool(annotations=_SEND)(remarkable_form_send)
        mcp.tool(annotations=_SEND)(remarkable_ask)
