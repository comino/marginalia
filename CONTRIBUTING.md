# Contributing

Thanks for helping! Marginalia is a small codebase with a big test suite; the
suite is what lets the ink classifiers change safely.

## Setup

```bash
uv sync --all-extras
uv run pytest -q                                   # offline, about a minute
uv run ruff check . && uv run ruff format --check .
```

No tablet is needed: tests synthesise pen ink as real v6 `.rm` files and swap
the cloud for a fake (`FakeCloud` in `tests/test_workflows.py`).

## Ground rules

- **Every behaviour change comes with a test** that fails without it. For ink
  classifiers, add the case to `tests/test_adversarial.py` (or the feature's
  test file) with synthesised strokes.
- **Never commit real user ink** or documents as fixtures, and never commit
  credentials (`~/.rmapi`, TRMNL UUIDs, cookies).
- Tool names (`remarkable_*`, `trmnl_*`) and the import name `remarkable_mcp`
  are stable API; agents key on them.
- Python ≥ 3.10, line length 100 (tests may be longer).
- Open a pull request against `main`; CI must be green (Python 3.10–3.12, lint,
  MCP conformance).

## Where things live

`remarkable_mcp/core/` core tools · `transports/` cloud, SSH, USB, local cache ·
`documents/` extraction and exports · `workflows/<workflow>/` the ink-reading
workflows (engine next to its `*tools` module) · `trmnl/` the independent TRMNL
tools. [CLAUDE.md](CLAUDE.md) lists the non-obvious facts (coordinates, rate
limits, sync behaviour).

## The README gallery

The hero animation and the gallery are generated from the real engines:

```bash
uv run --with Hershey-Fonts python docs/assets/make_gallery.py
```

Rerun it when a change alters what the gallery shows.
