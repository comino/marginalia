"""TRMNL private-plugin webhook client plus the e-ink content rules.

API (https://docs.trmnl.com/go/private-plugins/webhooks):
  POST {api_base}/custom_plugins/{uuid}
       {"merge_variables": {...}, "merge_strategy": "default|deep_merge|stream", "stream_limit": N}
       - payload max 2 KB (5 KB on TRMNL+), 12 pushes/hour (30 on TRMNL+), 429 when exceeded
  GET  {api_base}/custom_plugins/{uuid}    -> {"merge_variables": {...}}
  POST {api_base}/plugin_settings/{uuid}/image  (raw image body, Content-Type image/png|jpeg|bmp)

The display template used here renders six text slots, message1 .. message6,
each at most three lines separated by <br>. Those rules are enforced in
`build_slot_text`, so agents cannot push content that wraps or garbles.
"""

from __future__ import annotations

import fcntl
import io
import json
import re
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from . import __version__
from .config import Config

# TRMNL sits behind Cloudflare, which rejects generic library user agents
# (Python-urllib/x.y gets a 403 "error 1010"), so every request identifies itself.
USER_AGENT = f"remarkable-mcp-trmnl/{__version__} (+https://github.com/comino/remarkable-mcp)"

SLOT_COUNT = 6
MAX_LINES_PER_SLOT = 3
MAX_CHARS_PER_LINE = 45
MAX_IMAGE_BYTES = 90 * 1024
IMAGE_SIZE = (800, 480)
MERGE_STRATEGIES = ("default", "deep_merge", "stream")

# Emoji, pictographs, dingbats, variation selectors, ZWJ, keycap combiner.
# TRMNL's e-ink font renders these as garbage glyphs, so they are removed.
_EMOJI_RE = re.compile(
    "[\U0001f000-\U0001ffff"  # emoji, symbols & pictographs, supplemental
    "☀-➿"  # misc symbols + dingbats (⚠ ✅ ❌ ☀ ...)
    "⬀-⯿"  # misc symbols & arrows (⭐ ...)
    "︀-️"  # variation selectors
    "‍⃣"  # ZWJ, keycap
    "\U0001f1e6-\U0001f1ff"  # regional indicators (flags)
    "]"
)
_WS_RE = re.compile(r"[ \t]+")


class TrmnlError(Exception):
    """Base error; message is safe to surface to the calling agent."""


class RateLimited(TrmnlError):
    pass


class PayloadTooLarge(TrmnlError):
    pass


class ContentError(TrmnlError):
    pass


@dataclass
class HttpResponse:
    status: int
    body: str


HttpFn = Callable[[str, str, bytes | None, dict[str, str]], HttpResponse]
"""(method, url, body, headers) -> HttpResponse — injectable for tests."""


def _urllib_http(
    method: str, url: str, body: bytes | None, headers: dict[str, str]
) -> HttpResponse:
    req = urllib.request.Request(
        url, data=body, method=method, headers={"User-Agent": USER_AGENT, **headers}
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return HttpResponse(resp.status, resp.read().decode("utf-8", "replace"))
    except urllib.error.HTTPError as e:
        return HttpResponse(e.code, e.read().decode("utf-8", "replace"))
    except urllib.error.URLError as e:
        raise TrmnlError(f"Network error talking to TRMNL: {e.reason}") from e


# --------------------------------------------------------------------------- content rules


def sanitize_line(text: str) -> str:
    """Strip emoji/pictographs and collapse whitespace. Keeps umlauts, °, –, →."""
    text = _EMOJI_RE.sub("", text)
    text = _WS_RE.sub(" ", text)
    return text.strip()


def truncate(text: str, limit: int = MAX_CHARS_PER_LINE) -> str:
    if len(text) <= limit:
        return text
    return text[: limit - 3].rstrip() + "..."


def normalize_lines(value: str | list[str]) -> list[str]:
    """Accept a list of lines, or a string with newlines / <br> separators."""
    if isinstance(value, str):
        raw = re.split(r"<br\s*/?>|\n", value)
    else:
        raw = [str(v) for v in value]
    lines = [sanitize_line(line) for line in raw]
    return [line for line in lines if line]


def build_slot_text(value: str | list[str], *, strict: bool = False) -> str:
    """Turn agent input into one slot's text: max 3 lines of max 45 chars, joined by <br>.

    strict=True raises instead of truncating, for agents that want to be told.
    """
    lines = normalize_lines(value)
    if len(lines) > MAX_LINES_PER_SLOT:
        if strict:
            raise ContentError(f"slot has {len(lines)} lines, max is {MAX_LINES_PER_SLOT}")
        lines = lines[:MAX_LINES_PER_SLOT]
    out = []
    for line in lines:
        if len(line) > MAX_CHARS_PER_LINE:
            if strict:
                raise ContentError(
                    f"line is {len(line)} chars, max is {MAX_CHARS_PER_LINE}: {line!r}"
                )
            line = truncate(line)
        out.append(line)
    return "<br>".join(out)


def slot_key(slot: int | str) -> str:
    """Accept 1..6, "1".."6", "message1".."message6" -> "messageN"."""
    s = str(slot).strip().lower()
    if s.startswith("message"):
        s = s[len("message") :]
    if not s.isdigit() or not (1 <= int(s) <= SLOT_COUNT):
        raise ContentError(f"slot must be 1..{SLOT_COUNT} (got {slot!r})")
    return f"message{int(s)}"


def render_preview(merge_variables: dict[str, Any]) -> str:
    """Text rendering of the six slots roughly as the display shows them."""
    width = MAX_CHARS_PER_LINE + 4
    rule = "+" + "-" * width + "+"
    out = [rule]
    for i in range(1, SLOT_COUNT + 1):
        text = merge_variables.get(f"message{i}", "")
        lines = [ln for ln in re.split(r"<br\s*/?>", str(text)) if ln] or ["(leer)"]
        for j, line in enumerate(lines):
            tag = f"[{i}]" if j == 0 else "   "
            out.append(f"| {tag} {line:<{width - 5}}|")
        out.append(rule)
    extra = {k: v for k, v in merge_variables.items() if not re.fullmatch(r"message[1-6]", k)}
    if extra:
        out.append("other merge variables: " + json.dumps(extra, ensure_ascii=False))
    return "\n".join(out)


# --------------------------------------------------------------------------- rate limiting


class PushLog:
    """Local record of POSTs in the last hour, shared across agents via a lock file.

    TRMNL counts every payload against the hourly quota and answers 429 after
    that, and a 429 gives no retry-after. Refusing locally before the request
    keeps one chatty agent from locking everyone out of the display.
    """

    def __init__(self, state_dir: Path, limit_per_hour: int):
        self.path = state_dir / "pushes.json"
        self.lock_path = state_dir / "pushes.lock"
        self.limit = limit_per_hour

    def _read(self) -> list[float]:
        try:
            stamps = json.loads(self.path.read_text())
        except (FileNotFoundError, json.JSONDecodeError):
            return []
        cutoff = time.time() - 3600
        return [t for t in stamps if isinstance(t, (int, float)) and t > cutoff]

    def recent(self) -> list[float]:
        return self._read()

    def remaining(self) -> int:
        return max(0, self.limit - len(self._read()))

    def _wait_seconds(self, stamps: list[float]) -> int:
        """Seconds until the oldest counted push ages out; 0 when a slot is free."""
        if len(stamps) < self.limit:
            return 0
        if not stamps:  # limit <= 0: nothing will ever free up
            return 3600
        oldest_counted = sorted(stamps)[-self.limit] if self.limit > 0 else max(stamps)
        return int(max(0, oldest_counted + 3600 - time.time())) + 1

    def seconds_until_slot(self) -> int:
        return self._wait_seconds(self._read())

    def reserve(self, force: bool = False) -> None:
        """Atomically check the quota and record one push. Raises RateLimited."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with open(self.lock_path, "w") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            stamps = self._read()
            if len(stamps) >= self.limit and not force:
                wait = self._wait_seconds(stamps)
                raise RateLimited(
                    f"Local quota exhausted: {len(stamps)}/{self.limit} pushes in the last hour. "
                    f"Next slot frees in ~{wait}s. Batch several slot updates into one call, "
                    "or pass force=true if you know the server-side counter is lower."
                )
            stamps.append(time.time())
            self.path.write_text(json.dumps(stamps))


# --------------------------------------------------------------------------- client


class TrmnlClient:
    def __init__(self, config: Config, http: HttpFn | None = None):
        self.config = config
        self.http = http or _urllib_http
        self.pushlog = PushLog(config.state_dir, config.rate_limit_per_hour)

    # -- URLs
    @property
    def plugin_url(self) -> str:
        return f"{self.config.api_base}/custom_plugins/{self.config.plugin_uuid}"

    @property
    def image_url(self) -> str | None:
        if not self.config.image_plugin_uuid:
            return None
        return f"{self.config.api_base}/plugin_settings/{self.config.image_plugin_uuid}/image"

    # -- read
    def get(self) -> dict[str, Any]:
        resp = self.http("GET", self.plugin_url, None, {"Accept": "application/json"})
        if resp.status != 200:
            raise TrmnlError(self._describe_error(resp))
        try:
            data = json.loads(resp.body)
        except json.JSONDecodeError as e:
            raise TrmnlError(f"TRMNL returned non-JSON: {resp.body[:200]}") from e
        mv = data.get("merge_variables")
        return mv if isinstance(mv, dict) else {}

    # -- write
    def encode_payload(
        self,
        merge_variables: dict[str, Any],
        merge_strategy: str = "default",
        stream_limit: int | None = None,
    ) -> bytes:
        if merge_strategy not in MERGE_STRATEGIES:
            raise ContentError(f"merge_strategy must be one of {MERGE_STRATEGIES}")
        body: dict[str, Any] = {"merge_variables": merge_variables}
        if merge_strategy != "default":
            body["merge_strategy"] = merge_strategy
        if stream_limit is not None:
            if merge_strategy != "stream":
                raise ContentError("stream_limit only applies with merge_strategy='stream'")
            body["stream_limit"] = int(stream_limit)
        raw = json.dumps(body, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        if len(raw) > self.config.max_payload_bytes:
            raise PayloadTooLarge(
                f"payload is {len(raw)} bytes, TRMNL accepts at most "
                f"{self.config.max_payload_bytes}. Shorten the content or send fewer slots."
            )
        return raw

    def push(
        self,
        merge_variables: dict[str, Any],
        merge_strategy: str = "default",
        stream_limit: int | None = None,
        *,
        force: bool = False,
    ) -> dict[str, Any]:
        raw = self.encode_payload(merge_variables, merge_strategy, stream_limit)
        self.pushlog.reserve(force=force)
        resp = self.http("POST", self.plugin_url, raw, {"Content-Type": "application/json"})
        if resp.status == 429:
            raise RateLimited(
                f"TRMNL answered 429 (max {self.config.rate_limit_per_hour} pushes/hour). "
                "Wait before pushing again."
            )
        if resp.status != 200:
            raise TrmnlError(self._describe_error(resp))
        return {
            "status": "ok",
            "bytes": len(raw),
            "merge_strategy": merge_strategy,
            "pushes_left_this_hour": self.pushlog.remaining(),
        }

    def push_image(self, path: str | Path, *, force: bool = False) -> dict[str, Any]:
        url = self.image_url
        if not url:
            raise TrmnlError(
                "No image_plugin_uuid configured (the /plugin_settings/<uuid>/image endpoint "
                "uses a different UUID than the webhook). Add it to the config file."
            )
        p = Path(path).expanduser()
        if not p.is_file():
            raise ContentError(f"file not found: {p}")
        data, content_type, note = prepare_image(p)
        self.pushlog.reserve(force=force)
        resp = self.http("POST", url, data, {"Content-Type": content_type})
        if resp.status == 429:
            raise RateLimited("TRMNL answered 429 for the image upload. Wait before retrying.")
        if resp.status != 200:
            raise TrmnlError(self._describe_error(resp))
        return {
            "status": "ok",
            "bytes": len(data),
            "content_type": content_type,
            "note": note,
            "pushes_left_this_hour": self.pushlog.remaining(),
        }

    @staticmethod
    def _describe_error(resp: HttpResponse) -> str:
        detail = resp.body.strip()
        try:
            detail = json.loads(detail).get("error", detail)
        except (json.JSONDecodeError, AttributeError):
            pass
        if resp.status == 422:
            return f"TRMNL rejected the payload (422): {detail or 'invalid payload'}"
        if resp.status == 404:
            return "TRMNL returned 404 — the plugin UUID in the config is wrong or the plugin was deleted."
        return f"TRMNL error {resp.status}: {detail[:300]}"


def prepare_image(path: Path) -> tuple[bytes, str, str]:
    """Return (bytes, content_type, note). Re-encodes to 800x480 1-bit PNG when needed."""
    ext = path.suffix.lower().lstrip(".")
    ct = {"png": "image/png", "jpg": "image/jpeg", "jpeg": "image/jpeg", "bmp": "image/bmp"}.get(
        ext
    )
    raw = path.read_bytes()
    if ct and len(raw) <= MAX_IMAGE_BYTES and _is_display_size(raw):
        return raw, ct, "sent as-is"

    try:
        from PIL import Image
    except ImportError as e:  # pragma: no cover
        raise TrmnlError("Pillow is required to convert images") from e
    try:
        img = Image.open(io.BytesIO(raw))
    except Exception as e:
        raise ContentError(f"cannot decode image {path.name}: {e}") from e

    img = img.convert("L")
    img.thumbnail(IMAGE_SIZE)
    canvas = Image.new("L", IMAGE_SIZE, 255)
    canvas.paste(img, ((IMAGE_SIZE[0] - img.width) // 2, (IMAGE_SIZE[1] - img.height) // 2))
    out = io.BytesIO()
    canvas.convert("1").save(out, format="PNG", optimize=True)
    data = out.getvalue()
    if len(data) > MAX_IMAGE_BYTES:
        raise PayloadTooLarge(
            f"image is still {len(data) // 1024} KB after conversion (limit {MAX_IMAGE_BYTES // 1024} KB); "
            "simplify the image."
        )
    return data, "image/png", f"converted {path.name} -> 800x480 1-bit PNG, {len(data) // 1024} KB"


def _is_display_size(raw: bytes) -> bool:
    try:
        from PIL import Image

        return Image.open(io.BytesIO(raw)).size == IMAGE_SIZE
    except Exception:
        return False
