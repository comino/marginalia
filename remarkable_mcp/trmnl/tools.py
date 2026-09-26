"""TRMNL tools, registered on the reMarkable MCP server when TRMNL is configured."""

from __future__ import annotations

import json
import logging
import os
from typing import Any

from mcp.types import ToolAnnotations

from remarkable_mcp.trmnl.client import (
    MAX_CHARS_PER_LINE,
    MAX_LINES_PER_SLOT,
    MERGE_STRATEGIES,
    SLOT_COUNT,
    TrmnlClient,
    TrmnlError,
    build_slot_text,
    render_preview,
    sanitize_line,
    slot_key,
)
from remarkable_mcp.trmnl.config import ConfigError, config_path, load_config

log = logging.getLogger(__name__)

INSTRUCTIONS = f"""\
## TRMNL e-ink display (trmnl_* tools)

An 800x480 black/white display with {SLOT_COUNT} text slots (message1..message{SLOT_COUNT}),
top to bottom. Max {MAX_LINES_PER_SLOT} lines per slot, {MAX_CHARS_PER_LINE} characters per line,
no emoji. Only 12 pushes per hour for the whole display: batch slots into one
trmnl_set_slots call and use dry_run=true to preview.
"""

_READ = ToolAnnotations(read_only_hint=True, open_world_hint=True)
_WRITE = ToolAnnotations(
    read_only_hint=False, destructive_hint=False, idempotent_hint=True, open_world_hint=True
)
_REPLACE = ToolAnnotations(
    read_only_hint=False, destructive_hint=True, idempotent_hint=True, open_world_hint=True
)

_client: TrmnlClient | None = None


def get_client() -> TrmnlClient:
    global _client
    if _client is None:
        _client = TrmnlClient(load_config())
    return _client


def _fail(e: Exception) -> str:
    """Errors are returned as text so the calling agent can act on them."""
    return f"ERROR: {e}"


# --------------------------------------------------------------------------- read tools


def trmnl_status() -> str:
    """
    <usecase>Check the TRMNL connection: config source, remaining push budget this hour, and what the display currently shows.</usecase>
    """
    try:
        c = get_client()
        current = c.get()
        info = {
            "config_source": c.config.source,
            "plugin_url": c.plugin_url,
            "image_upload_configured": c.image_url is not None,
            "pushes_left_this_hour": c.pushlog.remaining(),
            "pushes_last_hour": len(c.pushlog.recent()),
            "next_push_slot_in_seconds": c.pushlog.seconds_until_slot(),
            "limits": {
                "pushes_per_hour": c.config.rate_limit_per_hour,
                "payload_bytes": c.config.max_payload_bytes,
                "slots": SLOT_COUNT,
                "lines_per_slot": MAX_LINES_PER_SLOT,
                "chars_per_line": MAX_CHARS_PER_LINE,
            },
        }
        return json.dumps(info, indent=2, ensure_ascii=False) + "\n\n" + render_preview(current)
    except (ConfigError, TrmnlError) as e:
        return _fail(e)


def trmnl_get() -> str:
    """
    <usecase>Return the raw merge variables currently stored for the display, as JSON.</usecase>
    """
    try:
        return json.dumps(get_client().get(), indent=2, ensure_ascii=False)
    except (ConfigError, TrmnlError) as e:
        return _fail(e)


def trmnl_dashboard() -> str:
    """
    <usecase>Show the six dashboard slots as they are on the display right now (text preview).</usecase>
    """
    try:
        return render_preview(get_client().get())
    except (ConfigError, TrmnlError) as e:
        return _fail(e)


# --------------------------------------------------------------------------- dashboard slots


def trmnl_set_slots(
    slots: dict[str, str | list[str]],
    dry_run: bool = False,
    strict: bool = False,
    force: bool = False,
) -> str:
    """
    <usecase>Update one or more of the six dashboard slots; untouched slots keep their content.</usecase>
    <instructions>
    slots: mapping of slot -> content. Keys are "1".."6" (or "message1".."message6").
    Values are a list of lines, or a string with newlines. Max 3 lines of 45 chars each;
    emoji are stripped, longer lines are truncated (or rejected when strict=true).
    Use a value of "" to blank a slot.

    Batch every slot you want to change into one call: each call costs one of the
    12 pushes per hour. dry_run=true returns the rendering without pushing.
    force=true bypasses the local quota check (the server may still answer 429).
    </instructions>
    """
    try:
        c = get_client()
        if not slots:
            return _fail(ValueError("slots is empty"))
        merge: dict[str, str] = {}
        for key, value in slots.items():
            merge[slot_key(key)] = build_slot_text(value, strict=strict)

        if dry_run:
            preview_vars = dict(c.get())
            preview_vars.update(merge)
            return "DRY RUN — nothing pushed. Display would show:\n" + render_preview(preview_vars)

        result = c.push(merge, merge_strategy="deep_merge", force=force)
        return json.dumps({**result, "updated": merge}, ensure_ascii=False, indent=2)
    except (ConfigError, TrmnlError, ValueError) as e:
        return _fail(e)


def trmnl_set_slot(slot: int, lines: list[str], dry_run: bool = False, force: bool = False) -> str:
    """
    <usecase>Update a single dashboard slot (1..6) with up to 3 short lines. Other slots are kept.</usecase>
    <instructions>
    Prefer trmnl_set_slots when changing more than one slot — every call is one push of the 12/hour.
    </instructions>
    """
    return trmnl_set_slots({str(slot): lines}, dry_run=dry_run, force=force)


def trmnl_clear_slots(slots: list[int] | None = None, force: bool = False) -> str:
    """
    <usecase>Blank the given dashboard slots (1..6), or all six when no list is given.</usecase>
    """
    try:
        targets = slots or list(range(1, SLOT_COUNT + 1))
        merge = {slot_key(s): "" for s in targets}
        result = get_client().push(merge, merge_strategy="deep_merge", force=force)
        return json.dumps({**result, "cleared": sorted(merge)}, ensure_ascii=False, indent=2)
    except (ConfigError, TrmnlError) as e:
        return _fail(e)


# --------------------------------------------------------------------------- generic pushes


def trmnl_send(title: str, message: str, force: bool = False) -> str:
    """
    <usecase>Push a simple title + message. Replaces ALL merge variables (the six slots are wiped).</usecase>
    <instructions>
    Only renders if the plugin's markup uses {{ title }} / {{ message }}. The current dashboard
    template renders message1..message6, so prefer trmnl_set_slots for that layout.
    </instructions>
    """
    try:
        merge = {"title": sanitize_line(title), "message": sanitize_line(message)}
        result = get_client().push(merge, force=force)
        return json.dumps({**result, "sent": merge}, ensure_ascii=False, indent=2)
    except (ConfigError, TrmnlError) as e:
        return _fail(e)


def trmnl_send_list(title: str, items: list[str], force: bool = False) -> str:
    """
    <usecase>Push a title + list of items as {title, items:[{name}]}. Replaces ALL merge variables.</usecase>
    <instructions>
    Only renders if the plugin's markup iterates {{ items }}.
    </instructions>
    """
    try:
        merge = {
            "title": sanitize_line(title),
            "items": [{"name": sanitize_line(i)} for i in items],
        }
        result = get_client().push(merge, force=force)
        return json.dumps({**result, "sent": merge}, ensure_ascii=False, indent=2)
    except (ConfigError, TrmnlError) as e:
        return _fail(e)


def trmnl_push(
    merge_variables: dict[str, Any],
    merge_strategy: str = "default",
    stream_limit: int | None = None,
    dry_run: bool = False,
    force: bool = False,
) -> str:
    """
    <usecase>Push arbitrary merge variables to the plugin (escape hatch; no content rules applied).</usecase>
    <instructions>
    merge_strategy: "default" replaces everything, "deep_merge" merges keys into the existing
    data, "stream" appends to top-level arrays (bounded by stream_limit). Payload max 2 KB.
    dry_run=true only validates and reports the encoded size.
    </instructions>
    """
    try:
        c = get_client()
        if merge_strategy not in MERGE_STRATEGIES:
            return _fail(ValueError(f"merge_strategy must be one of {MERGE_STRATEGIES}"))
        raw = c.encode_payload(merge_variables, merge_strategy, stream_limit)
        if dry_run:
            return (
                f"DRY RUN — valid payload, {len(raw)} bytes (limit {c.config.max_payload_bytes})."
            )
        result = c.push(merge_variables, merge_strategy, stream_limit, force=force)
        return json.dumps(result, ensure_ascii=False, indent=2)
    except (ConfigError, TrmnlError, ValueError) as e:
        return _fail(e)


def trmnl_clear(force: bool = False) -> str:
    """
    <usecase>Wipe the display: replaces all merge variables with six empty slots and an empty title/message.</usecase>
    """
    try:
        merge = {f"message{i}": "" for i in range(1, SLOT_COUNT + 1)}
        merge.update({"title": "", "message": ""})
        result = get_client().push(merge, force=force)
        return json.dumps(result, ensure_ascii=False, indent=2)
    except (ConfigError, TrmnlError) as e:
        return _fail(e)


def trmnl_image(path: str, force: bool = False) -> str:
    """
    <usecase>Show a full-screen image (png/jpg/bmp) on the display.</usecase>
    <instructions>
    Anything not already 800x480 and under 90 KB is converted to a centered
    800x480 1-bit PNG. Requires image_plugin_uuid in the config.
    </instructions>
    """
    try:
        return json.dumps(get_client().push_image(path, force=force), ensure_ascii=False, indent=2)
    except (ConfigError, TrmnlError) as e:
        return _fail(e)


_TOOLS = [
    (trmnl_status, _READ),
    (trmnl_get, _READ),
    (trmnl_dashboard, _READ),
    (trmnl_set_slots, _WRITE),
    (trmnl_set_slot, _WRITE),
    (trmnl_clear_slots, _WRITE),
    (trmnl_send, _REPLACE),
    (trmnl_send_list, _REPLACE),
    (trmnl_push, _REPLACE),
    (trmnl_clear, _REPLACE),
    (trmnl_image, _REPLACE),
]


def configured() -> bool:
    """TRMNL tools are offered only when a display is configured on this machine."""
    return bool(os.environ.get("TRMNL_PLUGIN_UUID")) or config_path().is_file()


def register(mcp) -> None:
    for fn, hints in _TOOLS:
        mcp.tool(annotations=hints)(fn)
