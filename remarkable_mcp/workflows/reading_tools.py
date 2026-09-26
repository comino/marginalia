"""Reading queue: clip web articles to the tablet, get highlights back as quotes."""

from __future__ import annotations

import asyncio
import uuid
from typing import List, Optional

from mcp.types import ToolAnnotations

from remarkable_mcp.api import get_items_by_id
from remarkable_mcp.responses import make_error, make_response
from remarkable_mcp.workflows import cloud, handwriting
from remarkable_mcp.workflows.ink import load_document_ink_from_zip
from remarkable_mcp.workflows.review import collect_requests
from remarkable_mcp.workflows.review_pdf import render_review_pdf
from remarkable_mcp.workflows.state import Store, now_iso, slugify
from remarkable_mcp.workflows.web import (
    Article,
    extract_article,
    fetch_html,
    text_fragment_link,
)

DEFAULT_FOLDER = "/Reading"

_WRITE = ToolAnnotations(
    title="Clip Article to Tablet",
    read_only_hint=False,
    destructive_hint=False,
    idempotent_hint=False,
    open_world_hint=True,  # fetches the web
)
_READ = ToolAnnotations(
    title="Reading Notes",
    read_only_hint=False,  # remembers which marks were returned (local state)
    destructive_hint=False,
    idempotent_hint=False,
    open_world_hint=False,
)
_LIST = ToolAnnotations(
    title="Reading Queue",
    read_only_hint=True,
    destructive_hint=False,
    idempotent_hint=True,
    open_world_hint=False,
)


def _store() -> Store:
    return Store("reading")


async def remarkable_clip(
    url: Optional[str] = None,
    html: Optional[str] = None,
    markdown: Optional[str] = None,
    title: Optional[str] = None,
    folder: str = DEFAULT_FOLDER,
) -> str:
    """
    <usecase>Send a web article to the tablet as a clean, annotatable PDF.</usecase>
    <instructions>
    Fetches the page, strips navigation/ads/comments, and renders the article
    text with numbered paragraphs and a note margin. The user reads and marks
    it up on the tablet; remarkable_reading_notes() returns the highlights and
    notes as quotes with deep links back to the source passage.
    Pass `html` if you already fetched the page, or `markdown` for text that
    is already clean (keep `url` for the links).
    </instructions>
    <parameters>
    - url: Article URL.
    - html: Page HTML (skips fetching).
    - markdown: Already extracted article text.
    - title: Override the detected title.
    - folder: Tablet folder (default "/Reading").
    </parameters>
    <examples>
    - remarkable_clip("https://example.com/some-essay")
    </examples>
    """
    if not (url or html or markdown):
        return make_error("invalid_arguments", "Provide url, html or markdown.", "Pass url=...")

    def work():
        if markdown:
            art = Article(title=title or "Article", markdown=markdown, url=url, extractor="given")
        else:
            page = html if html is not None else fetch_html(url)
            art = extract_article(page, url)
        if title:
            art.title = title
        subtitle = " · ".join(x for x in (art.site, art.author, art.date) if x)
        with cloud.MUPDF_LOCK:
            rendered = render_review_pdf(
                art.markdown, title=art.title, subtitle=subtitle, legend=False
            )
        name = art.title[:90]
        doc = cloud.upload_pdf(rendered.pdf, name, folder)
        return art, rendered, doc

    if not cloud.is_cloud():
        return make_error(
            "unsupported_transport",
            "Clipping uploads through the cloud sync API only.",
            "Run the server in cloud mode (the default).",
        )
    try:
        art, rendered, doc = await asyncio.to_thread(work)
    except Exception as exc:
        return make_error("clip_failed", f"Could not clip the article: {exc}", "Check the URL.")

    item_id = f"{slugify(art.title, 40)}-{uuid.uuid4().hex[:4]}"
    _store().put(
        item_id,
        {
            "id": item_id,
            "title": art.title,
            "url": art.url,
            "author": art.author,
            "date": art.date,
            "site": art.site,
            "extractor": art.extractor,
            "doc_id": doc.id,
            "doc_name": art.title[:90],
            "folder": folder,
            "sent_at": now_iso(),
            "ink_at_send": cloud.ink_token(doc),
            "blocks": rendered.manifest_blocks(),
            "layout": rendered.layout,
            "source_text": art.markdown,
            "seen_strokes": [],
            "quotes": {},
        },
    )
    return make_response(
        {
            "item": item_id,
            "title": art.title,
            "pages": rendered.page_count,
            "paragraphs": len(rendered.blocks),
            "words": len(art.markdown.split()),
            "extractor": art.extractor,
        },
        f"On the tablet in {folder}. Later: remarkable_reading_notes('{item_id}').",
    )


def _quote_from_request(req, note_text: Optional[str], url: Optional[str]) -> dict:
    m = req.mark
    block = req.block or {}
    if m.kind in ("margin_bar", "note") or not m.target_text:
        quote = block.get("text", "") if m.kind != "note" or not m.target_text else m.target_text
    else:
        quote = m.target_text
    quote = " ".join(quote.split())
    out = {
        "id": m.id,
        "kind": m.kind,
        "quote": quote if len(quote) < 700 else quote[:697] + "…",
        "paragraph": int(block["id"][1:]) if block.get("id") else None,
        "note": note_text,
        "link": text_fragment_link(url, quote if m.kind != "note" else ""),
    }
    return out


def _digest(item: dict, quotes: List[dict]) -> str:
    lines = [f"## {item['title']}", ""]
    if item.get("url"):
        lines += [item["url"], ""]
    for q in quotes:
        if q["kind"] == "note" and q.get("note"):
            lines.append(f"- **Note** (¶{q['paragraph']}): {q['note']}")
            continue
        src = f"[¶{q['paragraph']}]({q['link']})" if q.get("link") else f"¶{q['paragraph']}"
        lines.append(f"> {q['quote']}")
        lines.append(f"> — {src}")
        if q.get("note"):
            lines.append(f"\n  Note: {q['note']}")
        lines.append("")
    return "\n".join(lines).strip() + "\n"


async def remarkable_reading_notes(
    item: Optional[str] = None,
    only_new: bool = True,
    include_images: bool = False,
    mark_seen: bool = True,
):
    """
    <usecase>Get highlights, underlines and margin notes from clipped articles.</usecase>
    <instructions>
    Returns quotes (the marked text, or the paragraph for margin bars/notes),
    the handwritten note next to them, the paragraph number and a deep link
    that scrolls to the passage in the original article (URL text fragment),
    plus a ready-to-paste Markdown digest. Without `item`, every clipped
    article with new ink is processed.
    </instructions>
    <parameters>
    - item: Reading item id (from remarkable_clip / remarkable_reading_list).
    - only_new: Only marks not returned before (default true).
    - include_images: Attach crops of handwritten notes.
    - mark_seen: Remember returned marks (default true).
    </parameters>
    """
    store = _store()
    records = [store.get(item)] if item else list(store.all())
    if item and records[0] is None:
        return make_error(
            "item_not_found", f"No reading item '{item}'.", "Use remarkable_reading_list()."
        )

    def work():
        c = cloud.client()
        cloud.refresh(c)
        results = []
        for rec in records:
            doc = cloud.find_by_id(c, rec["doc_id"])
            if doc is None:
                continue
            if not item and cloud.ink_token(doc) == (rec.get("last_read") or {}).get(
                "ink", rec.get("ink_at_send")
            ):
                continue  # nothing new on this one
            zip_bytes = cloud.download_zip(c, doc)
            with cloud.MUPDF_LOCK:
                ink = load_document_ink_from_zip(zip_bytes)
                reqs = collect_requests(
                    ink,
                    rec["blocks"],
                    rec["source_text"],
                    set(rec.get("seen_strokes", [])),
                    rec["layout"],
                )
            if only_new:
                reqs = [r for r in reqs if r.new]
            results.append((rec, doc, reqs))
        return results

    try:
        results = await asyncio.to_thread(work)
    except Exception as exc:
        return make_error("reading_failed", str(exc), "Check remarkable_status().")

    engine = handwriting.backend()
    items_out = []
    images = []
    for rec, doc, reqs in results:
        notes = [(r, r.mark if r.mark.kind == "note" else r.mark.note) for r in reqs]
        crops = [
            handwriting.render_strokes_png(n.strokes, n.rect) if n is not None else None
            for _, n in notes
        ]
        todo = [i for i, c in enumerate(crops) if c is not None]
        texts = handwriting.transcribe_many([crops[i] for i in todo], engine)
        text_at = {i: t for i, (t, _) in zip(todo, texts)}
        quotes = [
            _quote_from_request(r, text_at.get(i), rec.get("url")) for i, (r, _) in enumerate(notes)
        ]
        if include_images:
            images += [(quotes[i]["id"], "note", crops[i]) for i in todo]

        def merge(current, reqs=reqs, quotes=quotes, doc=doc):
            if current is None:
                return None
            if mark_seen:
                seen = set(current.get("seen_strokes", []))
                for r in reqs:
                    seen.update(r.mark.seen_keys)
                current["seen_strokes"] = sorted(seen)
            current.setdefault("quotes", {}).update({q["id"]: q for q in quotes})
            current["last_read"] = {"at": now_iso(), "ink": cloud.ink_token(doc)}
            return current

        store.update(rec["id"], merge)
        if quotes or item:
            items_out.append(
                {
                    "item": rec["id"],
                    "title": rec["title"],
                    "url": rec.get("url"),
                    "quotes": quotes,
                    "markdown": _digest(rec, quotes) if quotes else None,
                }
            )

    total = sum(len(i["quotes"]) for i in items_out)
    payload = make_response(
        {"items": items_out, "handwriting_backend": engine},
        f"{total} quote(s) from {len(items_out)} article(s)." if total else "No new highlights.",
    )
    return cloud.with_images(payload, images) if images else payload


async def remarkable_reading_list() -> str:
    """
    <usecase>List clipped articles and which have new highlights or notes.</usecase>
    """
    records = list(_store().all())
    if not records:
        return make_response({"items": []}, "Nothing clipped yet. Use remarkable_clip(url).")

    def work():
        c = cloud.client()
        cloud.refresh(c)
        by_id = get_items_by_id(c.get_meta_items())
        rows = []
        for r in records:
            baseline = (r.get("last_read") or {}).get("ink") or r.get("ink_at_send")
            status, location = cloud.doc_status(by_id.get(r["doc_id"]), baseline, by_id)
            rows.append(
                {
                    "item": r["id"],
                    "title": r["title"],
                    "url": r.get("url"),
                    "sent_at": r["sent_at"],
                    "status": status,
                    "location": location,
                    "quotes_collected": len(r.get("quotes", {})),
                }
            )
        return rows

    try:
        rows = await asyncio.to_thread(work)
    except Exception as exc:
        return make_error("list_failed", str(exc), "Check remarkable_status().")
    new = [r["item"] for r in rows if r["status"] in ("annotated", "done")]
    return make_response(
        {"items": rows},
        f"New marks on: {', '.join(new)}." if new else "No new marks.",
    )


def register(mcp, write_enabled: bool) -> None:
    mcp.tool(annotations=_READ)(remarkable_reading_notes)
    mcp.tool(annotations=_LIST)(remarkable_reading_list)
    if write_enabled:
        mcp.tool(annotations=_WRITE)(remarkable_clip)
