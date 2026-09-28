# Changelog

Releases are tagged `marginalia-X.Y.Z` (plain `vX.Y.Z` tags come from the
upstream remarkable-mcp history). Newest first.

## Unreleased

- **Sketch:** a single box counts (the sketch tool and live watch no longer
  need three shapes or an edge before they report anything).
- **Sketch, from a live test on the tablet:**
  - tall letters closing a narrow outline inside a line of writing are text, not a box;
  - a separate arrowhead drawn *ahead* of the line's end makes the edge directed;
  - arrows can point at notes: each written line is a note with an id (`t1`, `t2`, …), and
    Mermaid draws pointed-at notes as flag nodes;
  - a label hanging over a box's edge (a closing "?") belongs to the box;
  - joined-up cursive words are writing, not dropped connectors.
- **Docs:** the tablet syncs when a document is closed or a page is left, not while the pen
  moves; live watch docs and tool descriptions say so.
- **README:** animated hero and a gallery generated from the real engines
  (`docs/assets/make_gallery.py`).

## 1.2.0 — 2026-09-26

- **TRMNL is an independent tool set.** Agents call the `trmnl_*` tools directly
  ([docs/trmnl.md](docs/trmnl.md)); nothing mirrors tablet state onto the display any more.
- **Autopilot** only starts the configured agent, and exits when no `agent_command` is set.
  The old `trmnl_slot` / `trmnl_min_interval_seconds` keys are ignored with a warning.
- **Code layout:** `remarkable_mcp` is organised into `core/`, `transports/`, `documents/`,
  `workflows/<workflow>/` and `trmnl/`. Tool names, import name and console scripts are unchanged.

## 1.1.2 / 1.1.1 — 2026-09-26

- **Autopilot:** SIGTERM (systemd stop/restart) is a clean shutdown: the run loop is cancelled
  from inside, the live watcher closes its socket (bounded), exit code 0.

## 1.1.0 — 2026-09-26

- **Security:** tool parameters that reach local files or the network are vetted: sensitive
  locations refused, suffix and size caps, optional `REMARKABLE_ALLOWED_ROOTS`, SSRF guard
  including tailnet addresses, redirects re-checked hop by hop.
- **Ink classifiers hardened by adversarial testing:** split, wavy, short and overshooting
  strikes and underlines; circles drawn as arcs; label circling, off-centre and crossed ticks
  on forms; multi-pull and wavy cancels and dividers in the inbox; sketch shapes fitted rather
  than corner-counted, with filled, in-stroke and one-barb arrowheads.
- **Tests:** MCP end-to-end, metamorphic invariants, adversarial regression tests, docs
  consistency; about 1080 offline tests on Python 3.10–3.12.
- **Performance:** dense pages (2000 strokes) analyse about 2.5× faster.

## 1.0.0 — 2026-09-26

The first release of **Marginalia**: pen review with source lines, code review to GitHub
comments, LaTeX review via SyncTeX, forms / questions / triage sheets, clarify on paper, the
Agent Inbox, sketches → Mermaid and SVG, tables, wireframes and math, the reading queue, the
ink digest, live watch and the autopilot. Builds on
[remarkable-mcp](https://github.com/SamMorrowDrums/remarkable-mcp) by Sam Morrow.
