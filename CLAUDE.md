# CLAUDE.md — Marginalia

MCP server that turns a reMarkable tablet into an agent front end. Fork of
SamMorrowDrums/remarkable-mcp (core: transports, sync client, extraction) plus
a workflow layer (`remarkable_mcp/workflows/`) and a TRMNL client
(`remarkable_mcp/trmnl/`). README.md is the overview; `docs/workflows.md`
documents every workflow tool; `docs/core.md` the core tools.

## Working here

- `uv sync --all-extras && uv run pytest -q` — ~1080 offline tests, ~60 s. Gate
  commits on pytest's exit code (not on `| tail`).
- `uv run ruff check . && uv run ruff format --check .` — line length 100;
  long lines are allowed only in `tests/` and the ported `trmnl/` code.
- Python ≥ 3.10 (CI matrix 3.10–3.12): no 3.12-only syntax (nested same-quote
  f-strings etc.).
- The package import name stays `remarkable_mcp` and the MCP server/tool names
  stay `remarkable` / `remarkable_*` on purpose: clients key tool names on them.
  The distribution is `marginalia` (console scripts `marginalia`,
  `marginalia-autopilot`, plus the old `remarkable-*` aliases).

## Facts not obvious from the code

- **Coordinates:** ink is mapped to PDF points with `x = rm_x*ppu + page_w/2`,
  `y = rm_y*ppu` (`ink.py`), `ppu` from the page's SceneInfo grid. Verified on
  real device markup. PyMuPDF word boxes span ascender→descender: use the
  estimated baseline, not box edges (`marks._horizontal_target`). PyMuPDF
  splits one visual line into several "blocks" — group words geometrically.
- **Sync API rate limit:** ~30 requests per short window per account, shared by
  every client. Metadata refreshes must be coalesced (`live.Watcher`); a bug
  that refreshed every 3 s plus a reconnect loop produced 15×429/min.
- **Notification socket:** `wss://internal.cloud.remarkable.com/notifications/ws/json/1`
  with `Authorization: Bearer <user token>`; the server drops it every few
  minutes; messages only say *that* a device synced — diff stroke-file hashes
  from metadata (`cloud.ink_token`, `live.snapshot`) to know *what*.
- A freshly uploaded document object has no file index: its ink baseline is
  `cloud.EMPTY_INK`, never `ink_token(doc)`.
- MuPDF is not thread-safe: wrap render/analysis in `cloud.MUPDF_LOCK`, but
  never hold it across network calls (OCR).
- "Seen" tracking is per stroke (`Mark.seen_keys`), keyed by PDF page / tablet
  page id — so a note added next to an already collected mark comes back.
- Tests live in `tests/` and never touch real state: `tests/conftest.py` points
  `REMARKABLE_WORKFLOW_STATE`, `TRMNL_STATE_DIR` and `REMARKABLE_AUTOPILOT_CONFIG` at temp dirs. Transport is
  swapped in one place (`workflows.cloud`, see `FakeCloud` in tests/test_workflows.py).
- Secrets: the reMarkable token lives in `~/.rmapi`; the TRMNL plugin UUID (a
  write credential) in `~/.config/trmnl/config.json`. Never print, log or put
  them in tests/fixtures; `trmnl_status` masks the UUID.
- Real user documents (the maintainer's annotated PDFs) must not become
  fixtures in this public repo; synthesise ink instead (`tests/test_workflows.py`
  helpers write real v6 `.rm` files).

## Deploying on this machine

The maintainer's Claude Code registers the server as `remarkable` via
`uvx --from "marginalia[web] @ git+https://github.com/comino/marginalia@main"`.
After pushing to main, run the same `uvx` command once with
`--refresh-package marginalia` or new sessions may use a cached build. The
autopilot runs as the systemd user service `remarkable-autopilot`, pinned to a
release tag (`contrib/remarkable-autopilot.service`). Release tags are
`marginalia-X.Y.Z` — plain `vX.Y.Z` tags are inherited from upstream and point at upstream code.
