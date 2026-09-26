"""Sidecar state for workflows, kept on the machine running the server.

Nothing here is written to the tablet. Each workflow record is one JSON file
under ``$REMARKABLE_WORKFLOW_STATE`` (default
``$XDG_STATE_HOME/remarkable-mcp``, i.e. ``~/.local/state/remarkable-mcp``),
grouped by kind: ``reviews/<slug>.json``, ``forms/<id>.json``, ...

Writes are atomic (temp file + rename) so a crash never leaves half a record.
"""

from __future__ import annotations

import json
import os
import re
import tempfile
import unicodedata
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterator, Optional


def state_root() -> Path:
    override = os.environ.get("REMARKABLE_WORKFLOW_STATE")
    if override:
        return Path(override).expanduser()
    base = os.environ.get("XDG_STATE_HOME") or str(Path.home() / ".local" / "state")
    return Path(base) / "remarkable-mcp"


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def slugify(text: str, max_len: int = 60) -> str:
    text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode()
    text = re.sub(r"[^a-zA-Z0-9]+", "-", text).strip("-").lower()
    return text[:max_len].rstrip("-") or "untitled"


class Store:
    """JSON records of one kind (e.g. "reviews")."""

    def __init__(self, kind: str, root: Optional[Path] = None):
        self.dir = (root or state_root()) / kind

    def _path(self, key: str) -> Path:
        if not re.fullmatch(r"[A-Za-z0-9._-]+", key):
            raise ValueError(f"Invalid record key: {key!r}")
        return self.dir / f"{key}.json"

    def get(self, key: str) -> Optional[Dict[str, Any]]:
        path = self._path(key)
        if not path.exists():
            return None
        return json.loads(path.read_text())

    def put(self, key: str, record: Dict[str, Any]) -> None:
        self.dir.mkdir(parents=True, exist_ok=True)
        record["updated_at"] = now_iso()
        fd, tmp = tempfile.mkstemp(dir=self.dir, prefix=f".{key}.", suffix=".tmp")
        try:
            with os.fdopen(fd, "w") as f:
                json.dump(record, f, indent=2, ensure_ascii=False)
            os.replace(tmp, self._path(key))
        except BaseException:
            Path(tmp).unlink(missing_ok=True)
            raise

    def all(self) -> Iterator[Dict[str, Any]]:
        if not self.dir.exists():
            return
        for path in sorted(self.dir.glob("*.json")):
            try:
                yield json.loads(path.read_text())
            except ValueError:
                continue

    def find(self, predicate) -> Optional[Dict[str, Any]]:
        return next((r for r in self.all() if predicate(r)), None)
