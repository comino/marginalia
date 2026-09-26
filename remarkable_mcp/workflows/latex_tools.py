"""MCP tools: review compiled LaTeX with the pen, get .tex file:line requests back."""

from __future__ import annotations

import asyncio
import json
import shutil
import uuid
from pathlib import Path
from typing import Optional

from mcp.types import ToolAnnotations

from remarkable_mcp.responses import make_error, make_response
from remarkable_mcp.workflows import cloud, handwriting
from remarkable_mcp.workflows.ink import load_document_ink_from_zip
from remarkable_mcp.workflows.latex_review import (
    collect_tex_requests,
    synctex_available,
    synctex_file,
    synctex_inputs,
)
from remarkable_mcp.workflows.state import Store, now_iso, slugify, state_root

DEFAULT_FOLDER = "/Review/LaTeX"

_SEND = ToolAnnotations(
    title="Send LaTeX PDF for Review",
    read_only_hint=False,
    destructive_hint=False,
    idempotent_hint=False,
    open_world_hint=False,
)
_COLLECT = ToolAnnotations(
    title="Collect LaTeX Review",
    read_only_hint=False,
    destructive_hint=False,
    idempotent_hint=False,
    open_world_hint=False,
)


def _store() -> Store:
    return Store("latex-reviews")


async def remarkable_latex_review_send(
    pdf_path: str, title: Optional[str] = None, folder: str = DEFAULT_FOLDER
) -> str:
    """
    <usecase>Send a compiled LaTeX PDF (thesis, paper) to the tablet for pen review.</usecase>
    <instructions>
    The PDF must have been compiled with SyncTeX (pdflatex/lualatex/latexmk
    -synctex=1), so a <name>.synctex.gz sits next to it. The PDF and its
    SyncTeX data are snapshotted, so marks map to the sources of *this*
    compilation even after you edit and recompile. The PDF is uploaded
    unchanged. remarkable_latex_review_collect returns every mark with the
    .tex file and line it came from.
    </instructions>
    <parameters>
    - pdf_path: Compiled PDF (with .synctex.gz next to it).
    - title: Document name on the tablet (default: file name).
    - folder: Tablet folder (default "/Review/LaTeX").
    </parameters>
    <examples>
    - remarkable_latex_review_send("/home/me/thesis/thesis.pdf", title="Thesis draft 3")
    </examples>
    """
    from remarkable_mcp.workflows.safety import UnsafeInput, check_local_file

    try:
        pdf = check_local_file(pdf_path, (".pdf",), 300_000_000, "PDF")
    except UnsafeInput as exc:
        return make_error("invalid_pdf", str(exc), "Pass the compiled PDF.")
    sync = synctex_file(pdf)
    if sync is None:
        return make_error(
            "no_synctex",
            f"No {pdf.stem}.synctex.gz next to the PDF.",
            "Recompile with -synctex=1 (e.g. latexmk -pdf -synctex=1) and try again.",
        )
    if not synctex_available():
        return make_error(
            "synctex_missing", "The synctex CLI is not installed.", "Install TeX Live."
        )
    if not cloud.is_cloud():
        return make_error("unsupported_transport", "Uploads need cloud mode.", "Use cloud mode.")
    review_id = f"{slugify(title or pdf.stem, 40)}-{uuid.uuid4().hex[:4]}"
    snap = state_root() / "latex" / review_id
    snap.mkdir(parents=True, exist_ok=True)
    shutil.copy2(pdf, snap / pdf.name)
    shutil.copy2(sync, snap / sync.name)
    # Snapshot the .tex/.bib sources of this compilation too, so "line" and
    # "source" refer to the text that was actually printed, even after edits.
    sources = {}
    project = pdf.parent.resolve()
    for path in dict.fromkeys(synctex_inputs(sync)):
        p = Path(path)
        inside = project == p or project in p.parents  # skip texmf classes/packages
        if inside and p.suffix in (".tex", ".bib", ".sty", ".cls", ".bbl") and p.is_file():
            try:
                if p.stat().st_size <= 2_000_000:
                    sources[path] = p.read_text(errors="replace")
            except OSError:
                continue
    (snap / "sources.json").write_text(json.dumps(sources))
    name = title or pdf.stem

    try:
        doc = await asyncio.to_thread(cloud.upload_pdf, pdf.read_bytes(), name, folder)
    except Exception as exc:
        return make_error("upload_failed", str(exc), "Check remarkable_status().")
    _store().put(
        review_id,
        {
            "id": review_id,
            "title": name,
            "source_pdf": str(pdf),
            "snapshot_pdf": str(snap / pdf.name),
            "project_dir": str(pdf.parent),
            "doc_id": doc.id,
            "sent_at": now_iso(),
            "ink_at_send": cloud.EMPTY_INK,
            "seen_strokes": [],
        },
    )
    return make_response(
        {"review": review_id, "document": name, "folder": folder},
        f"On the tablet in {folder}. Later: remarkable_latex_review_collect('{review_id}').",
    )


async def remarkable_latex_review_collect(
    review: str, only_new: bool = True, include_images: bool = False, mark_seen: bool = True
):
    """
    <usecase>Turn pen marks on a LaTeX review into edits at .tex file:line.</usecase>
    <instructions>
    Every mark comes back with its kind/intent (delete, replace, change,
    comment, attention), the marked text, the handwritten note, and the
    source location resolved via SyncTeX: "file" (relative to the project
    directory when inside it, else absolute), "line", and "source" - the text
    of that line *as it was compiled* (snapshotted at send time). Check that
    the line still reads the same before editing a file you changed since.
    </instructions>
    <parameters>
    - review: Id from remarkable_latex_review_send.
    - only_new: Only marks not returned before (default true).
    - include_images: Attach crops of handwritten notes.
    - mark_seen: Remember returned marks (default true); false to peek again,
      e.g. with include_images=true to read untranscribed notes.
    </parameters>
    """
    store = _store()
    rec = store.get(review)
    if rec is None:
        return make_error("review_not_found", f"No LaTeX review '{review}'.", "Check the id.")

    def work():
        c = cloud.client()
        cloud.refresh(c)
        doc = cloud.find_by_id(c, rec["doc_id"])
        if doc is None:
            raise LookupError(rec["title"])
        zip_bytes = cloud.download_zip(c, doc)
        with cloud.MUPDF_LOCK:
            ink = load_document_ink_from_zip(zip_bytes)
        pages = {p.pdf_page + 1: p for p in ink.pages if p.pdf_page is not None}
        snap_sources = Path(rec["snapshot_pdf"]).parent / "sources.json"
        sources = json.loads(snap_sources.read_text()) if snap_sources.exists() else None
        reqs = collect_tex_requests(Path(rec["snapshot_pdf"]), pages, sources)
        seen = set(rec.get("seen_strokes", []))
        if only_new:
            reqs = [r for r in reqs if not set(r.mark.seen_keys) <= seen]
        notes = [r.mark if r.mark.kind == "note" else r.mark.note for r in reqs]
        crops = [handwriting.render_strokes_png(n.strokes, n.rect) if n else None for n in notes]
        todo = [i for i, cr in enumerate(crops) if cr]
        texts = handwriting.transcribe_many(
            [crops[i] for i in todo], strokes=[notes[i].strokes for i in todo]
        )
        return doc, reqs, crops, {i: t for i, (t, _) in zip(todo, texts)}

    try:
        doc, reqs, crops, texts = await asyncio.to_thread(work)
    except LookupError:
        return make_error("document_missing", "The review PDF is gone from the tablet.", "Resend.")
    except Exception as exc:
        return make_error("collect_failed", str(exc), "Check remarkable_status().")

    project = Path(rec["project_dir"])
    out, images = [], []
    for i, r in enumerate(reqs):
        m = r.mark
        file = r.file
        if file:
            try:
                file = str(Path(file).relative_to(project))
            except ValueError:
                pass
        item = {
            "id": m.id,
            "page": r.page,
            "kind": m.kind,
            "intent": m.intent,
            "target": m.target_text or None,
            "file": file,
            "line": r.line,
            "source": r.source,
        }
        if crops[i] is not None:
            item["note"] = texts.get(i)
            item["note_status"] = "transcribed" if texts.get(i) else "not_transcribed"
            if include_images:
                images.append((m.id, "note", crops[i]))
        out.append(item)

    def merge(cur):
        if cur is None:
            return None
        if mark_seen:
            seen = set(cur.get("seen_strokes", []))
            for r in reqs:
                seen.update(r.mark.seen_keys)
            cur["seen_strokes"] = sorted(seen)
            cur["last_read"] = {"at": now_iso(), "ink": cloud.ink_token(doc)}
        return cur

    store.update(review, merge)
    counts: dict = {}
    for item in out:
        counts[item["intent"]] = counts.get(item["intent"], 0) + 1
    hint = (
        f"{len(out)} request(s); file/line point into {rec['project_dir']}."
        if out
        else "No new marks."
    )
    if any(i.get("note_status") == "not_transcribed" for i in out) and not include_images:
        hint += (
            " Some notes are untranscribed: call again with include_images=true, "
            "only_new=false, mark_seen=false to read them."
        )
    payload = make_response(
        {
            "review": review,
            "project_dir": rec["project_dir"],
            "handwriting_backend": handwriting.backend(),
            "counts": counts,
            "requests": out,
        },
        hint,
    )
    return cloud.with_images(payload, images) if images else payload


def register(mcp, write_enabled: bool) -> None:
    mcp.tool(annotations=_COLLECT)(remarkable_latex_review_collect)
    if write_enabled:
        mcp.tool(annotations=_SEND)(remarkable_latex_review_send)
