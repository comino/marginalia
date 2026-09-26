# Workflow Tools

The core tools expose what is on the tablet. The workflow tools interpret it:
they look at the ink, work out what it means relative to the printed text, and
answer with small structured results. The geometry, anchoring and bookkeeping
happen in the server, so a small model can run the workflows with a few calls.

| Tool | What it does |
|------|--------------|
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
     "src_line": 15, "note": "own DB per chat?", "note_status": "transcribed:claude"}
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
`done` once it is moved to a folder named `Done` or `Answered`.

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
| `auto` (default) | `google` if `GOOGLE_VISION_API_KEY` is set, else `claude` if `ANTHROPIC_API_KEY` is set, else `none` |
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
forms/<form>.json            field geometry, last answers
inbox/<name>.json            entries (fingerprints, status, text, replies)
handwriting-cache/<hash>.json
```

Status checks compare hashes of the stroke files from the document metadata, so
they need no download. Opening a document on the tablet doesn't count as a
change.

`REMARKABLE_REVIEW_FOLDER` changes the default upload folder (`/Review`).

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
