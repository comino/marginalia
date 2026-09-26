"""One call that tells an agent what needs attention across all workflows."""

from __future__ import annotations

import asyncio

from mcp.types import ToolAnnotations

from remarkable_mcp.api import get_items_by_id
from remarkable_mcp.responses import make_error, make_response
from remarkable_mcp.workflows import cloud
from remarkable_mcp.workflows.state import Store

_READ = ToolAnnotations(
    title="What's New on the Tablet",
    read_only_hint=True,
    destructive_hint=False,
    idempotent_hint=True,
    open_world_hint=False,
)

_NEXT_STEP = {
    "reviews": "remarkable_review_collect('{id}')",
    "forms": "remarkable_form_read('{id}')",
    "reading": "remarkable_reading_notes('{id}')",
    "inbox": "remarkable_inbox(name='{id}')",
}


def _baseline(kind: str, rec: dict):
    if kind == "reviews":
        latest = rec["versions"][-1]
        return (
            latest["doc_id"],
            (rec.get("last_collect") or {}).get("ink") or latest.get("ink_at_send"),
            latest["doc_name"],
        )
    if kind == "forms":
        return (
            rec["doc_id"],
            (rec.get("last_read") or {}).get("ink") or rec.get("ink_at_send"),
            rec["title"],
        )
    if kind == "reading":
        return (
            rec["doc_id"],
            (rec.get("last_read") or {}).get("ink") or rec.get("ink_at_send"),
            rec["title"],
        )
    # inbox: any ink change since the last scan (a never-scanned inbox counts from empty)
    return (
        rec.get("doc_id"),
        (rec.get("last_scan") or {}).get("ink") or cloud.EMPTY_INK,
        rec.get("document"),
    )


async def remarkable_whats_new() -> str:
    """
    <usecase>See everything on the tablet that needs the agent's attention, in one call.</usecase>
    <instructions>
    Checks every tracked workflow without downloading documents (stroke-file
    hashes from metadata only):
    - reviews with new pen marks (or moved to a Reviewed/Done folder)
    - forms and questions with new answers
    - Agent Inbox with new handwriting
    - clipped articles with new highlights
    Each item names the tool call that fetches the details. Start a session
    with this, then call the suggested tools.
    </instructions>
    """
    kinds = ("reviews", "forms", "inbox", "reading")
    try:
        records = {k: list(Store(k).all()) for k in kinds}
    except Exception as exc:  # unreadable state dir: report, don't crash callers
        return make_error("state_unreadable", str(exc), "Check ~/.local/state/remarkable-mcp.")
    if not any(records.values()):
        return make_response(
            {"attention": [], "tracked": 0},
            "Nothing is tracked yet. Start with remarkable_review_send, remarkable_ask, "
            "remarkable_inbox_setup or remarkable_clip.",
        )

    def work():
        c = cloud.client()
        cloud.refresh(c)
        by_id = get_items_by_id(c.get_meta_items())
        attention, waiting, missing = [], 0, 0
        for kind in kinds:
            for rec in records[kind]:
                doc_id, baseline, name = _baseline(kind, rec)
                if not doc_id:
                    continue
                status, location = cloud.doc_status(by_id.get(doc_id), baseline, by_id)
                key = rec.get("slug") or rec.get("id") or rec.get("name")
                if kind == "inbox":
                    pending = sum(
                        1 for e in rec.get("entries", {}).values() if e.get("status") == "pending"
                    )
                    if status in ("annotated", "done") or pending:
                        attention.append(
                            {
                                "kind": "inbox",
                                "id": key,
                                "title": name,
                                "status": status,
                                "known_pending": pending,
                                "next": _NEXT_STEP[kind].format(id=key),
                            }
                        )
                    elif status == "missing":
                        missing += 1
                    else:
                        waiting += 1
                    continue
                if status in ("annotated", "done"):
                    attention.append(
                        {
                            "kind": kind.rstrip("s") if kind != "reading" else "article",
                            "id": key,
                            "title": name,
                            "status": status,
                            "location": location,
                            "next": _NEXT_STEP[kind].format(id=key),
                        }
                    )
                elif status == "missing":
                    missing += 1
                else:
                    waiting += 1
        return attention, waiting, missing

    try:
        attention, waiting, missing = await asyncio.to_thread(work)
    except Exception as exc:
        return make_error("overview_failed", str(exc), "Check remarkable_status().")
    tracked = sum(len(v) for v in records.values())
    hint = (
        f"{len(attention)} item(s) need attention; call the 'next' tool for each."
        if attention
        else "Nothing new. Everything sent is still waiting for the user."
    )
    return make_response(
        {
            "attention": attention,
            "waiting": waiting,
            "missing": missing,
            "tracked": tracked,
        },
        hint,
    )


def register(mcp, write_enabled: bool) -> None:
    del write_enabled
    mcp.tool(annotations=_READ)(remarkable_whats_new)
