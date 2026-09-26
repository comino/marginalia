"""Near-live change stream from the reMarkable cloud.

The sync service pushes a ``SyncComplete`` notification over a websocket every
time a device finishes a sync (while you write, the tablet syncs every few
seconds). A notification does not say *what* changed, so the watcher keeps a
metadata snapshot - per document, the hash of every stroke file - and diffs it
after each notification. The result is a stream of ``Change`` events naming
the document and the exact pages that got new ink.

Protocol (as used by the official apps and rmapi-js)::

    wss://<sync host>/notifications/ws/json/1   Authorization: Bearer <user token>
    {"message": {"attributes": {"event": "SyncComplete", "sourceDeviceID": ..}}}

The server drops the socket every few minutes; the watcher reconnects with
backoff and falls back to polling when the socket is unavailable (non-cloud
transports), so consumers never have to care.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Callable, Deque, Dict, List, Optional, Tuple

from remarkable_mcp.api import get_item_path, get_items_by_id
from remarkable_mcp.workflows import cloud

logger = logging.getLogger(__name__)

NOTIFICATIONS_PATH = "/notifications/ws/json/1"
POLL_SECONDS = 60.0
# The sync API rate-limits (HTTP 429, ~30 requests per short window, shared by
# every client of the account). While someone writes, the tablet syncs every
# few seconds; refreshes are coalesced to one per window, and a sync that
# arrives during a refresh schedules another, so the last change of a burst is
# never missed.
MIN_REFRESH_SECONDS = 10.0
# A socket that closes sooner than this counts as a failed connection (backoff),
# so a server that accepts and immediately drops us cannot cause a tight loop.
HEALTHY_CONNECTION_SECONDS = 30.0
HISTORY = 500  # recent changes kept for resuming consumers (see changes_since)
# Per-subscriber backlog cap: a consumer that stops reading loses the oldest
# events instead of growing memory for days.
MAX_QUEUE = 500
MAX_BACKOFF_SECONDS = 300.0


@dataclass
class DocState:
    name: str
    path: str
    parent: str
    pages: Dict[str, str]  # stroke file id (<doc>/<page>.rm) -> hash
    trashed: bool


@dataclass
class Change:
    doc_id: str
    name: str
    path: str
    kind: str  # "ink" | "new" | "moved" | "removed"
    pages: List[str] = field(default_factory=list)  # tablet page ids with changed ink
    at: float = field(default_factory=time.time)

    def to_dict(self) -> dict:
        return {
            "document": self.name,
            "path": self.path,
            "doc_id": self.doc_id,
            "kind": self.kind,
            "changed_page_ids": self.pages,
            "at": time.strftime("%H:%M:%S", time.localtime(self.at)),
        }


def snapshot(c) -> Dict[str, DocState]:
    """Metadata-only view of every document's stroke files (no downloads)."""
    items = c.get_meta_items()
    by_id = get_items_by_id(items)
    out: Dict[str, DocState] = {}
    for item in items:
        if item.is_folder:
            continue
        files = getattr(item, "files", None) or []
        pages = {f["id"]: f.get("hash", "") for f in files if str(f.get("id", "")).endswith(".rm")}
        if not files:  # transports without a file index: whole-document granularity
            pages = {"*": getattr(item, "hash", "") or ""}
        out[item.ID] = DocState(
            name=item.VissibleName,
            path=get_item_path(item, by_id),
            parent=getattr(item, "Parent", "") or "",
            pages=pages,
            trashed=cloud.is_trashed(item, by_id),
        )
    return out


def diff(old: Dict[str, DocState], new: Dict[str, DocState]) -> List[Change]:
    changes: List[Change] = []
    for doc_id, cur in new.items():
        prev = old.get(doc_id)
        if prev is None:
            pages = sorted(_page_id(fid) for fid in cur.pages if fid != "*")
            changes.append(Change(doc_id, cur.name, cur.path, "new", pages))
            continue
        if cur.trashed and not prev.trashed:
            changes.append(Change(doc_id, cur.name, cur.path, "removed"))
            continue
        touched = sorted(
            _page_id(fid) for fid, h in cur.pages.items() if prev.pages.get(fid) != h
        ) + sorted(_page_id(fid) for fid in prev.pages if fid not in cur.pages)
        if touched:
            changes.append(Change(doc_id, cur.name, cur.path, "ink", touched))
        elif cur.parent != prev.parent:
            changes.append(Change(doc_id, cur.name, cur.path, "moved"))
    for doc_id, prev in old.items():
        if doc_id not in new and not prev.trashed:
            changes.append(Change(doc_id, prev.name, prev.path, "removed"))
    return changes


def _page_id(file_id: str) -> str:
    return file_id.rsplit("/", 1)[-1].removesuffix(".rm")


def _socket_url() -> str:
    from remarkable_mcp.sync import SYNC_HOST

    return SYNC_HOST.replace("https://", "wss://").replace("http://", "ws://") + NOTIFICATIONS_PATH


class Watcher:
    """Turns sync notifications into document/page Change events.

    ``run()`` loops forever; ``subscribe()`` gives each consumer its own queue.
    ``connect``/``take_snapshot`` are injectable for tests.
    """

    def __init__(
        self,
        take_snapshot: Optional[Callable[[], Dict[str, DocState]]] = None,
        connect=None,
        poll_seconds: float = POLL_SECONDS,
        min_refresh: Optional[float] = None,
    ):
        self._take_snapshot = take_snapshot or self._default_snapshot
        self._connect = connect or self._default_connect
        self.poll_seconds = poll_seconds
        self.min_refresh = MIN_REFRESH_SECONDS if min_refresh is None else min_refresh
        self.state: Optional[Dict[str, DocState]] = None
        self.mode = "starting"  # "socket" | "polling"
        self.last_event: Optional[float] = None
        self.events_seen = 0
        self._queues: List[asyncio.Queue] = []
        self._lock = asyncio.Lock()
        self._last_refresh = 0.0
        self._pump: Optional[asyncio.Task] = None
        self._dirty = False
        self._missing: Dict[str, int] = {}  # docs absent from the last snapshot(s)
        self.seq = 0  # sequence number of the latest published change
        self.history: Deque[Tuple[int, Change]] = deque(maxlen=HISTORY)
        self.errors = 0  # consecutive failed metadata reads (drives backoff)
        self.errors_total = 0  # failed metadata reads since start
        self.last_refresh_ok = True
        self.last_error: Optional[str] = None

    # ------------------------------------------------------------ consumers

    def subscribe(self) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue(maxsize=MAX_QUEUE)
        self._queues.append(q)
        return q

    def unsubscribe(self, q: asyncio.Queue) -> None:
        if q in self._queues:
            self._queues.remove(q)

    # ------------------------------------------------------------ core

    @staticmethod
    def _default_snapshot() -> Dict[str, DocState]:
        c = cloud.client()
        cloud.refresh(c)
        return snapshot(c)

    def changes_since(self, seq: int) -> List[Tuple[int, Change]]:
        """Published changes with a sequence number above ``seq`` (oldest first)."""
        return [(n, ch) for n, ch in self.history if n > seq]

    def request_refresh(self) -> None:
        """Ask for a refresh; coalesced to one per ``min_refresh`` window.

        A single pump task serves all requests: a request arriving while a
        refresh is running marks the state dirty, so the pump refreshes once
        more after it - the last sync of a burst is always picked up.
        """
        self._dirty = True
        if self._pump is None or self._pump.done():
            self._pump = asyncio.create_task(self._run_pump())

    async def _run_pump(self) -> None:
        while self._dirty:
            wait = max(0.0, self.min_refresh - (time.time() - self._last_refresh))
            if wait:
                await asyncio.sleep(wait)
            self._dirty = False
            await self.safe_refresh()
            if not self.last_refresh_ok:
                self._dirty = True  # keep the pending change; safe_refresh backed off

    async def refresh(self) -> List[Change]:
        """Re-read metadata, publish and return what changed since the last look."""
        async with self._lock:
            self._last_refresh = time.time()
            new = await asyncio.to_thread(self._take_snapshot)
            if self.state is None:
                self.state = new
                return []
            # A document missing from one snapshot may just have failed to load
            # (the client skips those); report "removed" only if it stays gone.
            for doc_id, prev in self.state.items():
                if doc_id in new:
                    self._missing.pop(doc_id, None)
                elif self._missing.get(doc_id, 0) < 1:
                    self._missing[doc_id] = 1
                    new[doc_id] = prev
            for doc_id in list(self._missing):
                if doc_id not in self.state:
                    self._missing.pop(doc_id, None)
            changes = diff(self.state, new)
            self.state = new
        for ch in changes:
            self.seq += 1
            self.history.append((self.seq, ch))
            for q in list(self._queues):
                if q.full():
                    q.get_nowait()  # drop the oldest event for a stalled consumer
                q.put_nowait(ch)
        return changes

    async def safe_refresh(self) -> List[Change]:
        """refresh() that never raises: errors are counted and backed off."""
        try:
            changes = await self.refresh()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self.errors += 1
            self.errors_total += 1
            self.last_error = f"{type(exc).__name__}: {exc}"
            wait = min(MAX_BACKOFF_SECONDS, 5.0 * 2 ** min(self.errors, 6))
            logger.warning("metadata refresh failed (%s); retrying in %.0fs", exc, wait)
            self.last_refresh_ok = False
            await asyncio.sleep(wait)
            return []
        self.errors = 0
        self.last_refresh_ok = True
        return changes

    @staticmethod
    async def _default_connect():
        import websockets
        from websockets.exceptions import InvalidStatus

        c = cloud.client()
        if not getattr(c, "user_token", ""):
            await asyncio.to_thread(c.renew_token)
        for attempt in (1, 2):
            try:
                return await websockets.connect(
                    _socket_url(),
                    additional_headers={"Authorization": f"Bearer {c.user_token}"},
                    open_timeout=20,
                    ping_interval=30,
                )
            except InvalidStatus as exc:
                status = getattr(exc.response, "status_code", None)
                if attempt == 1 and status in (401, 403):
                    await asyncio.to_thread(c.renew_token)  # expired session token
                    continue
                raise

    async def run(self) -> None:
        while self.state is None:  # the first snapshot is the baseline; keep trying
            await self.safe_refresh()
        failures = 0
        while True:
            try:
                ws = await self._connect()
            except Exception as exc:
                failures += 1
                self.mode = "polling"
                wait = min(300.0, self.poll_seconds * failures)
                logger.info("notification socket unavailable (%s); polling for %.0fs", exc, wait)
                await self._poll_for(wait)
                continue
            self.mode = "socket"
            opened = time.time()
            try:
                async for raw in ws:
                    if _is_sync_event(raw):
                        self.last_event = time.time()
                        self.events_seen += 1
                        self.request_refresh()
            except Exception as exc:  # routine server-side drops land here
                logger.debug("notification socket closed: %s", exc)
            finally:
                try:
                    # bounded: a stuck close handshake must not hold up a shutdown
                    await asyncio.wait_for(ws.close(), timeout=2)
                except Exception:
                    pass
            if time.time() - opened < HEALTHY_CONNECTION_SECONDS:
                failures += 1  # dropped right away: back off before reconnecting
                wait = min(MAX_BACKOFF_SECONDS, 2.0 * 2 ** min(failures, 7))
                logger.info(
                    "notification socket closed after %.0fs; waiting %.0fs",
                    time.time() - opened,
                    wait,
                )
                await asyncio.sleep(wait)
            else:
                failures = 0
            # Catch anything that synced while we were reconnecting (rate-limited).
            self.request_refresh()

    async def _poll_for(self, seconds: float) -> None:
        end = time.time() + seconds
        while time.time() < end:
            await asyncio.sleep(min(self.poll_seconds, max(0.0, end - time.time())))
            await self.safe_refresh()


def _is_sync_event(raw) -> bool:
    try:
        msg = json.loads(raw)
    except (TypeError, ValueError):
        return False
    attrs = (msg.get("message") or {}).get("attributes") or {}
    return attrs.get("event") == "SyncComplete"


# One watcher per server process, started on first use and stopped again when
# nobody has asked for live changes for IDLE_SHUTDOWN_SECONDS: every Claude
# session is its own server process, and each watcher spends shared API quota.
IDLE_SHUTDOWN_SECONDS = 600.0
_shared: Optional[Watcher] = None
_task: Optional[asyncio.Task] = None
_reaper: Optional[asyncio.Task] = None
_last_used = 0.0


async def _reap() -> None:
    global _task
    while True:
        await asyncio.sleep(30)
        idle = time.time() - _last_used
        if _task is not None and not _task.done() and idle > IDLE_SHUTDOWN_SECONDS:
            _task.cancel()
            _task = None
            if _shared is not None:
                _shared.mode = "stopped (idle)"
            return


def shutdown() -> None:
    """Stop the shared watcher (server shutdown)."""
    global _task, _reaper
    for t in (_task, _reaper):
        if t is not None and not t.done():
            t.cancel()
    _task = _reaper = None


async def shared_watcher() -> Watcher:
    global _shared, _task, _reaper, _last_used
    _last_used = time.time()
    if _shared is None:
        _shared = Watcher()
    if _task is None or _task.done():
        _task = asyncio.create_task(_shared.run())
    if _reaper is None or _reaper.done():
        _reaper = asyncio.create_task(_reap())
        # First snapshot, so the caller's wait starts from "now".
        for _ in range(100):
            if _shared.state is not None:
                break
            await asyncio.sleep(0.1)
    return _shared
