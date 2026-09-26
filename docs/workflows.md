# Workflow Tools

The core tools expose what is on the tablet. The workflow tools interpret it:
they look at the ink, work out what it means relative to the printed text, and
answer with small structured results. The geometry, anchoring and bookkeeping
happen in the server, so a small model can run the workflows with a few calls.

| Tool | What it does |
|------|--------------|
| `remarkable_whats_new` | One call: everything that needs attention across all workflows, with the next tool call for each |
| `remarkable_review_send` | Render a Markdown draft as a review PDF and upload it (write mode, cloud) |
| `remarkable_review_collect` | Turn the pen marks on a review into change requests with source lines |
| `remarkable_review_list` | Show review rounds and which have new marks waiting |
| `remarkable_annotations` | Interpret the pen marks on any annotated PDF or notebook |
| `remarkable_ask` | Ask one question on the tablet; the user answers with a tick (write mode, cloud) |
| `remarkable_form_send` | Put a form with checkboxes, choices, scales and write-in fields on the tablet |
| `remarkable_form_read` | Read a form's answers from where the ink is |
| `remarkable_form_list` | Show sent forms and which have new answers |
| `remarkable_inbox_setup` | Create (or register) the Agent Inbox document |
| `remarkable_inbox` | Get the handwritten requests from the inbox as tasks |
| `remarkable_inbox_done` | Mark requests done and optionally reply with a PDF on the tablet |
| `remarkable_sketch` | Hand-drawn boxes, circles, diamonds and arrows → graph + Mermaid + clean SVG |
| `remarkable_regions` | Split a page's ink into writing and drawing regions with crops |
| `remarkable_clip` | Send a web article to the tablet as a clean, annotatable PDF (write mode, cloud) |
| `remarkable_reading_notes` | Highlights and margin notes from clipped articles → quotes with deep links |
| `remarkable_reading_list` | Clipped articles and which have new marks |
| `remarkable_clarify` | Ask about an unclear mark; the question page shows the user their own mark |
| `remarkable_triage_send` | Tick sheet: many items (issues, PRs, mails), one option per row |
| `remarkable_code_review_send` / `_collect` | PR or git diff on paper → GitHub review comments on path:line + verdict |
| `remarkable_latex_review_send` / `_collect` | Compiled LaTeX on paper → edits at .tex file:line via SyncTeX |
| `remarkable_table` | Hand-drawn table → Markdown + CSV |
| `remarkable_wireframe` | Paper wireframe → HTML prototype |
| `remarkable_math` | Handwritten math → LaTeX |
| `remarkable_ink_digest` | What you wrote since yesterday, page by page |
| `remarkable_live_watch` / `_status` | Follow the tablet near-live: wait for strokes, get the changed pages analysed |

A separate program, `remarkable-autopilot`, runs these checks unattended; see
[Autopilot](#autopilot).

## Start here: `remarkable_whats_new`

Every workflow keeps a local record of what was sent. `remarkable_whats_new()`
checks all of them in one call, using the stroke-file hashes in the document
metadata rather than downloading anything. It lists what changed and the tool
call that fetches the details:

```json
{"attention": [
  {"kind": "review", "id": "duckdb-post", "status": "annotated", "next": "remarkable_review_collect('duckdb-post')"},
  {"kind": "form", "id": "question-ship-it-3f9a1c", "status": "annotated", "next": "remarkable_form_read('question-ship-it-3f9a1c')"}],
 "waiting": 4, "missing": 0, "tracked": 6}
```

The `tablet_check_in` MCP prompt runs this whole loop: check, act on each item,
acknowledge, and report. The `review_draft` and `ask_on_tablet` prompts start
the other workflows.

## Pen review round-trip

```text
draft.md ──review_send──▶ /Review/<title> · v1 ──(you mark it up)──▶ review_collect
    ▲                                                                   │
    └──── agent edits draft.md at src_line ◀── change requests ─────────┘
          review_send(review=…, responses=[…]) ──▶ v2 with change bars + responses page
```

### 1. Send

```python
remarkable_review_send(source_path="/home/me/blog/drafts/post.md")
```

The review PDF is built for marking up with a pen:

- a 3:4 page that the tablet shows at 1:1, so there is no zoom or letterboxing
- wide line spacing, so a strike-through or underline lands on one line only
- a number in the left margin for each paragraph, list item, quote, code block and table
- a blank right margin for handwritten comments
- a marking guide on the first page (version 1 only by default)

YAML front matter supplies the title and is left out of the rendered page. The
server keeps a manifest of every block's page position and source line range;
see [State](#state).

### 2. Mark it up on the tablet

| Mark | Read as | Intent |
|------|---------|--------|
| Line through words | `strikethrough` | `delete` (with a note nearby: `replace`) |
| Scribble over a passage | `scribble` | `delete` |
| Underline | `underline` | `attention` (with a note: `change`) |
| Loop around words | `circle` | `attention` (with a note: `change`) |
| Highlighter | `highlight` | `attention` (with a note: `change`) |
| Vertical line beside text | `margin_bar` | `attention` on those lines (with a note: `change`) |
| Handwriting | `note` | attached to the closest mark, or `comment` on the paragraph beside it |

A handwritten note joins a mark when it is written close to it, or when it
sits in the margin on the same lines as a mark in that paragraph. Long straight
lines that mark no text (dividers, lines under your own handwriting) are
ignored.

When you're done, you can move the document to a folder named `Reviewed`,
`Done` or `Erledigt`. `remarkable_review_list` then reports it as `done`.

### 3. Collect

```python
remarkable_review_collect("post")
```

```json
{
  "review": "post",
  "version": 1,
  "source_path": "/home/me/blog/drafts/post.md",
  "counts": {"delete": 1, "change": 1, "comment": 1},
  "requests": [
    {"id": "m1f0c2a9b", "page": 2, "kind": "strikethrough", "intent": "delete",
     "target": "permission to fetch data", "paragraph": 1, "src_lines": [6, 7],
     "src_line": 6, "context": "An agent can have permission to fetch data and …"},
    {"id": "m77d01e3c", "page": 2, "kind": "circle", "intent": "change",
     "target": "Each session gets", "paragraph": 4, "src_lines": [14, 15],
     "src_line": 15, "note": "own DB per chat?", "note_status": "transcribed", "note_engine": "claude"}
  ]
}
```

- `src_lines` is the paragraph's line range in the source file. `src_line` is
  the exact line of the marked text, when it can be found.
- Collect only returns marks it hasn't returned before, so you can collect after
  every reading session. Use `only_new=false, mark_seen=false` to get everything
  again.
- `include_images=true` attaches PNG crops: the handwriting on its own, and each
  mark shown with the page text under it.

### 4. Next version

```python
remarkable_review_send(
    source_path="/home/me/blog/drafts/post.md",
    responses=[{"id": "m1f0c2a9b", "status": "done", "reply": "reworded"}],
)
```

A second send of the same file (or of the same `review`) uploads `· v2`.
Paragraphs whose text changed get a black bar in the left margin. The
`responses` go on a closing page that quotes each original comment next to what
was done about it.

## Forms and questions

```python
remarkable_ask("Publish the DuckDB post on Tuesday?")                 # Yes / No + comment
remarkable_ask("Which title?", ["Disposable DuckDB", "SQL without a shell"])
remarkable_form_send("Weekly check-in", [
    {"id": "energy", "type": "scale", "label": "Energy", "min": 1, "max": 5},
    {"id": "focus", "type": "multi", "label": "Focus areas", "options": ["Thesis", "MyScore", "Climaid"]},
    {"id": "blockers", "type": "text", "label": "Blockers", "lines": 3},
])
remarkable_form_read("<form id>")
```

Choice answers need no handwriting recognition. The server knows where every
answer box is, and an option counts as selected when there is a tick or cross
inside its box or a loop around it. Filling a box solid undoes a tick. If a
single-choice field has several options marked, it comes back as `ambiguous`,
with the candidates listed. Write-in fields and margin remarks go through the
handwriting pipeline.

```json
{"answered": true,
 "values": {"answer": "Yes", "comment": "only after the intro rewrite"},
 "fields": [{"id": "answer", "type": "choice", "status": "answered", "value": "Yes"}]}
```

Field types are `checkbox`, `choice`, `multi`, `scale` and `text`, plus
`heading` and `info` for layout. Forms go to `/Agent/Forms`. A form counts as
`done` once it is moved to a done folder (see [State](#state)).

## Agent Inbox

`remarkable_inbox_setup()` uploads a ruled "Agent Inbox" PDF to `/Agent`.
Pass `document="My notebook"` to use an existing notebook instead. On the
inbox, you:

- write one request per block, with an empty line between requests
- strike a request through to cancel it (an underline under your writing is ignored)
- add `#tags` to route requests; they come back in `tags`

`remarkable_inbox()` returns the requests that need action: stable id, page,
text and tags. `remarkable_inbox_done(entries=[...], replies={id: "…"})`
acknowledges requests and uploads one "Replies" PDF to `/Agent/Replies`, so
the answers show up on the tablet. A request you add to after it was marked
done comes back as pending. Requests are recognised by their stroke
fingerprints, so extending one updates it rather than creating a new entry.

## Sketch → diagram

```python
remarkable_regions("Architecture ideas", page=2)          # find the drawing on the page
remarkable_sketch("Architecture ideas", page=2, region=[40, 300, 420, 560])
```

```json
{"nodes": [{"id": "n1", "shape": "rect", "label": "API"},
           {"id": "n2", "shape": "ellipse", "label": "DuckDB"},
           {"id": "n3", "shape": "diamond", "label": "authorised?"}],
 "edges": [{"from": "n1", "to": "n3", "directed": true, "label": null},
           {"from": "n3", "to": "n2", "directed": true, "label": "yes"}],
 "mermaid": "flowchart TD\n    n1[\"API\"]\n ..."}
```

Recognition is purely geometric:

- **Nodes.** Each outline is simplified with Ramer–Douglas–Peucker, and its
  corners are counted: 4 corners make a rectangle, or a diamond when the
  corners sit at the side midpoints; 3 make a triangle; a smooth closed curve
  is an ellipse. A box drawn in several strokes is joined up by its endpoints.
- **Edges.** Open strokes are lines. An arrowhead drawn in the same stroke, or
  as a separate small V at one end, makes the line an arrow. Each end snaps to
  the nearest shape boundary.
- **Labels.** Handwriting inside a shape names it; handwriting next to a line
  names the edge.
- **Ignored.** Lines that touch no shape, and loops back onto their own shape,
  are dropped; they are usually underlines or doubled outlines.

`include_svg=true` returns a clean redraw, and `include_images=true` also
attaches it as a PNG together with the label crops. `remarkable_regions` groups
a page's ink into separate regions, labels each `writing` or `drawing`, and
gives crops a small vision model can read one at a time.

## Reading queue

```python
remarkable_clip("https://example.com/essay")      # article -> /Reading as a clean PDF
remarkable_reading_notes()                         # every article with new marks
```

The article's main text is extracted (without navigation, ads or comments) and
rendered in the review layout, with numbered paragraphs and a note margin.
When you highlight, underline or circle something, or bracket lines with a
margin bar, it comes back as a quote. The quote includes the handwritten note
next to it and a link that jumps to the exact passage in the original article
(a `#:~:text=` URL text fragment). Each article also comes with a Markdown
digest you can paste into your notes:

```markdown
## Local-first ideas
https://example.com/lf

> Users own their data
> — [¶3](https://example.com/lf#:~:text=Users%20own%20their%20data)
```

Extraction uses [trafilatura](https://trafilatura.readthedocs.io/) when the
`web` extra is installed (`remarkable-mcp[web]`). Without it, a BeautifulSoup
fallback keeps the headings, paragraphs, lists, quotes and code from the main
content element.

## Live: follow the tablet as you write

The reMarkable sync service pushes a `SyncComplete` notification over a
websocket (`wss://…/notifications/ws/json/1`) every time the tablet syncs.
While you write, that happens every few seconds. The watcher keeps a snapshot
of every document's per-page stroke-file hashes, taken from metadata only, and
compares it against a fresh one after each notification. The result is a
stream of events: *document X, pages Y got new ink*.

```python
remarkable_live_watch("Whiteboard")    # blocks until you draw, then:
# {"status": "changed", "changes": [{"document": "Whiteboard", "changed_page_ids": [...]}],
#  "pages": [{"page": 1, "diagram": {"nodes": [...], "edges": [...], "mermaid": "..."}}]}
```

- Calling it in a loop lets an agent follow a sketching session.
- On PDFs the changed pages come back as annotations; on notebooks, as a
  diagram.
- `include_images` (on by default) attaches a render of each changed page.
- Refreshes are coalesced, because the sync API rate-limits at about 30
  requests per short window.
- Expired tokens are renewed when the socket rejects the connection.
- Without a socket (non-cloud transports), the watcher polls every 60 s.

reMarkable's own Screen Share is WebRTC through an undocumented broker, so it
isn't used here. Sync-level updates are robust and carry strokes rather than
pixels.

## Autopilot

`remarkable-autopilot` is a small daemon for this machine. It runs the same
watcher. Once your writing settles, it checks every tracked workflow (the
`remarkable_whats_new` logic) and, when something needs attention, starts a
headless agent with the `tablet_check_in` prompt, once per new state. What the
agent does then (reply on the tablet, open issues, post a line to a
[TRMNL display](trmnl.md)) is up to its prompt and tools; the autopilot itself
only knows the tablet.

Configuration lives in `~/.config/remarkable-mcp/autopilot.json`. Without an
`agent_command` there is nothing to do, and the daemon exits right away:

```json
{
  "agent_command": ["claude", "-p", "{prompt}", "--allowedTools", "mcp__remarkable"],
  "prompt": "optional; default: the tablet_check_in prompt",
  "debounce_seconds": 60,
  "min_agent_interval_seconds": 300,
  "agent_timeout_seconds": 900
}
```

The agent acts with your permissions while you're away, so switch it on
deliberately. Use `remarkable-autopilot --once` for a single dry-run check,
`--dry-run` to run without starting agents. Agent output is appended to
`~/.local/state/remarkable-mcp/autopilot.log`. A systemd user unit is in
`contrib/remarkable-autopilot.service`.

## Clarify on paper

When a request is ambiguous, `remarkable_clarify(request_id, question,
options, review=…)` puts a page on the tablet with a crop of the mark and the
printed text under it, the question as tick boxes, and room for a comment.
Read the answer with `remarkable_form_read`. The same `image` field type is
available in `remarkable_form_send` (`{"type": "image", "path": …}`).

## Triage sheets

`remarkable_triage_send(title, items, options)` renders compact rows: an item
title and subtitle on the left, the option boxes in columns on the right. Each
row reads back as a choice field, so the answers come from
`remarkable_form_read`. Handwriting next to a row comes back as a remark with
`near` set to that row's id. The `triage_on_paper` prompt drives the whole
loop against Linear or GitHub.

## Code review on paper

```python
remarkable_code_review_send(pr="42", repo="comino/remarkable-mcp")   # gh pr diff
remarkable_code_review_send(repo_path="/home/me/app", base="main")   # local range
remarkable_code_review_collect("<id>")
```

The diff is rendered like this:

- line numbers from the new side;
- added lines shaded, removed lines grey;
- a note margin;
- Approve / Request changes / Comment boxes at the end.

Every rendered row knows its `path`, `side` and `line`. A strike, circle or
underline on code, or a note in the margin, becomes a comment on that line.
The result includes a `github` object (`event`, `body`, `comments`) in the
pull-request review API format, ready for
`gh api repos/O/R/pulls/N/reviews --input …`. Posting it is left to the agent.

## LaTeX review via SyncTeX

Compile with SyncTeX (`latexmk -pdf -synctex=1`), then:

```python
remarkable_latex_review_send("/home/me/thesis/thesis.pdf", title="Thesis draft 3")
remarkable_latex_review_collect("<id>")
# [{"kind": "strikethrough", "intent": "delete", "target": "five random seeds.",
#   "file": "chapters/method.tex", "line": 4, "source": "Every configuration is ..."}]
```

The PDF goes to the tablet unchanged. The PDF and its SyncTeX data are
snapshotted, so marks keep mapping correctly after you edit and recompile.
Each mark is resolved with `synctex edit`, which follows `\input` and
`\include` into the right file, and the line is then refined using the
marked words. A margin note is resolved at the word next to it on its line.
Line starts often map to the paragraph's first source line, not to the
sentence the note is about.

## Tables, wireframes, math

- `remarkable_table` finds ruled rows and columns; a box around the table also
  counts. Handwriting is transcribed cell by cell and returned as Markdown and
  CSV.
- `remarkable_wireframe` maps shapes to UI roles: a box with an X is an image,
  a small labelled box is a button, a wide flat box is an input, a box holding
  others is a container, a circle is a round button, and loose handwriting is
  text. It returns an HTML prototype that keeps the sketch's layout, plus an
  element outline.
- `remarkable_math` splits the ink into blocks and returns LaTeX per block. It
  uses MyScript's math recogniser when configured, otherwise Claude with a
  LaTeX prompt.

## Daily ink digest

`remarkable_ink_digest(since_hours=24)` lists the documents modified in that
window and the pages whose stroke hashes changed since the previous digest.
For each page it returns the transcribed text blocks and a count of drawings.
The `daily_ink_digest` prompt summarises the result and suggests where each
note belongs.

## More prompts

`tablet_check_in`, `review_draft`, `ask_on_tablet`, `triage_on_paper`,
`meeting_pack` (agenda with note fields, then actions afterwards),
`research_on_paper` (`#research` inbox entries answered with a cited brief in
`/Reading`) and `daily_ink_digest`.

## Annotations on any document

`remarkable_annotations(document, pages=None, include_images=False)` runs the
same classifier on any PDF or notebook, such as a printed brief or a contract
draft. Marks are anchored to the PDF's own text layout: `region` names the text
block, and `target` holds the words that were marked.

## Handwriting transcription

Notes are cropped from the stroke data alone (black ink on white, without the
page underneath) and passed to a backend chosen by
`REMARKABLE_HANDWRITING_BACKEND`:

| Value | Backend |
|-------|---------|
| `auto` (default) | `myscript` if its keys are set, else `google` if `GOOGLE_VISION_API_KEY` is set, else `claude` if `ANTHROPIC_API_KEY` is set, else `none` |
| `myscript` | [MyScript iink](https://developer.myscript.com) stroke recognition, which reads the pen strokes themselves rather than an image. It is the most accurate option. Needs `MYSCRIPT_APPLICATION_KEY` and `MYSCRIPT_HMAC_KEY`; set the language with `MYSCRIPT_LANGUAGE` (default `en_US`, e.g. `de_DE`) |
| `google` | Google Cloud Vision document text detection |
| `claude` | Anthropic Messages API vision; model from `REMARKABLE_HANDWRITING_MODEL` (default `claude-haiku-4-5`) |
| `tesseract` | Local Tesseract (weak on handwriting) |
| `none` | No transcription. `note_status` is `not_transcribed` and the crops come back with `include_images=true` |

Transcriptions are cached by crop, so collecting the same marks again costs
nothing.

## State

Nothing is written to the tablet except the uploaded PDFs. Review manifests,
the marks already returned, and the transcription cache are JSON files under
`$REMARKABLE_WORKFLOW_STATE`, which defaults to `$XDG_STATE_HOME/remarkable-mcp`
(`~/.local/state/remarkable-mcp`):

```text
reviews/<review>.json        versions, block manifests, source text, seen strokes, requests
code-reviews/<id>.json       diff rows (path/side/line), verdict boxes, seen strokes
latex-reviews/<id>.json      snapshot paths, seen strokes
latex/<id>/                  snapshot of the PDF, its SyncTeX file and the .tex sources
forms/<form>.json            field geometry, last answers
inbox/<name>.json            entries (fingerprints, status, text, replies)
reading/<item>.json          article text, block manifest, quotes
ink-digest/last.json         page hashes already reported by the ink digest
handwriting-cache/<hash>.json
```

Snapshots under `latex/` are kept until you delete them. Deleting a record's
JSON file makes the server forget that workflow item; nothing on the tablet
changes.

Status checks compare hashes of the stroke files from the document metadata, so
they need no download. Opening a document on the tablet doesn't count as a
change.

`REMARKABLE_REVIEW_FOLDER` changes the default upload folder of Markdown reviews
(`/Review`). Code reviews go to `/Review/Code`, LaTeX reviews to `/Review/LaTeX`,
forms and questions to `/Agent/Forms`, clipped articles to `/Reading`. Every
send tool also takes an explicit `folder`.

**Done folders.** In every workflow, moving a document into a folder named
`Reviewed`, `Done`, `Erledigt`, `Answered` or `Beantwortet` (any case) marks
it as finished. Its status becomes `done` while there is unread ink, and
`collected` once that ink has been read.

## How marks are recognised

Each stroke is converted into PDF points using the same calibrated transform as
the page renderer, then described by a few geometric features:

- size, relative to the median word height
- straightness
- how often the stroke reverses direction
- closure: how near the end point is to the start
- ink density: path length divided by the box diagonal
- fill: the share of points in the central half of the box. Your circles have
  almost none; scribbles fill their box.

Horizontal strokes are scored against each text line's estimated baseline and
x-height middle, so an underline is not taken for a strike-through on the line
below. Text lines are grouped by position on the page, not by the PDF's block
and line numbers, which many PDF generators split up.
