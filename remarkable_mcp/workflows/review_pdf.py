"""Render Markdown into a PDF built for pen review on the tablet.

Compared with ``markdown_pdf`` (a plain A4 export) the review layout:

- uses a 3:4 page that the tablet shows at 1:1 (no zoom, no letterboxing),
- leaves a wide right margin for handwritten comments,
- uses generous line spacing so strike-throughs, underlines and interlinear
  corrections land unambiguously on one line,
- prints a small block number (``12``) in the left margin of every block, and
- returns a manifest mapping every block to its page position *and* the source
  Markdown lines it came from, so marks made on paper resolve to exact lines.

For later versions it can mark changed blocks with a bar in the left margin and
append a "responses" page answering the previous round's comments.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import asdict, dataclass, field
from datetime import datetime
from html import escape
from typing import Dict, List, Optional, Sequence, Tuple

import pymupdf
from markdown_it import MarkdownIt

# 1404 x 1872 device units at the calibrated 0.3177 pt/unit: shown 1:1 on the tablet.
PAGE_W = 446.0
PAGE_H = 595.0
MARGIN_LEFT = 38.0
MARGIN_TOP = 40.0
MARGIN_BOTTOM = 34.0
DEFAULT_NOTE_MARGIN = 118.0

_CSS = """
body { color: #111; font-family: serif; font-size: 10pt; line-height: 1.75; margin: 0; }
h1 { font-family: sans-serif; font-size: 17pt; line-height: 1.3; margin: 0 0 10pt; }
h2 { font-family: sans-serif; font-size: 13pt; line-height: 1.3; margin: 14pt 0 6pt; }
h3, h4, h5, h6 { font-family: sans-serif; font-size: 11pt; line-height: 1.3; margin: 12pt 0 5pt; }
p, ul, ol, blockquote, table { margin: 0 0 8pt; }
li { margin: 0 0 3pt; }
blockquote { border-left: 2px solid #777; color: #333; padding-left: 8pt; }
code { font-family: monospace; font-size: 8.5pt; }
pre { background-color: #f0f0f0; font-family: monospace; font-size: 8pt; line-height: 1.4;
      padding: 6pt; margin: 0 0 8pt; }
table { border-collapse: collapse; width: 100%; font-size: 8.5pt; line-height: 1.35; }
th, td { border: 1px solid #888; padding: 3pt; text-align: left; }
th { background-color: #e8e8e8; }
"""

_FRONTMATTER = re.compile(r"\A---[ \t]*\n(.*?\n)---[ \t]*\n", re.DOTALL)


@dataclass
class Block:
    """One reviewable unit of the rendered document."""

    id: str  # "p12" — the number printed in the margin is 12
    kind: str  # heading | paragraph | list_item | quote | code | table | hr
    src_lines: Tuple[int, int]  # 1-based inclusive line range in the source file
    text: str  # plain text, for matching and diffing between versions
    page: int = 0  # 1-based page where the block starts
    y: float = 0.0  # top of the block on that page (points)
    changed: bool = False  # new or edited since the previous version

    @property
    def number(self) -> int:
        return int(self.id[1:])

    @property
    def digest(self) -> str:
        return hashlib.sha1(" ".join(self.text.split()).encode()).hexdigest()[:12]


@dataclass
class ReviewRender:
    pdf: bytes
    blocks: List[Block]
    page_count: int
    title: str
    front_matter: Dict[str, str] = field(default_factory=dict)
    layout: Dict[str, float] = field(default_factory=dict)

    def manifest_blocks(self) -> List[dict]:
        return [asdict(b) | {"digest": b.digest} for b in self.blocks]


def split_front_matter(source: str) -> Tuple[Dict[str, str], str, int]:
    """Return (front matter scalars, body, number of lines removed)."""
    m = _FRONTMATTER.match(source)
    if not m:
        return {}, source, 0
    fm: Dict[str, str] = {}
    for line in m.group(1).splitlines():
        key, sep, value = line.partition(":")
        if sep and key.strip() and not line.startswith((" ", "\t", "-")):
            fm[key.strip()] = value.strip().strip("'\"")
    removed = m.group(0).count("\n")
    return fm, source[m.end() :], removed


def _plain(tokens) -> str:
    parts = []
    for t in tokens:
        if t.type == "inline":
            parts.append("".join(c.content for c in (t.children or []) if c.content))
        elif t.type in ("fence", "code_block"):
            parts.append(t.content)
    return " ".join(p.strip() for p in parts if p.strip())


_BLOCK_OPEN = {
    "heading_open": "heading",
    "paragraph_open": "paragraph",
    "list_item_open": "list_item",
    "blockquote_open": "quote",
    "table_open": "table",
}
_LEAF = {"fence": "code", "code_block": "code", "hr": "hr"}


def _assign_blocks(tokens, line_offset: int) -> List[Block]:
    """Tag reviewable tokens with ids; returns blocks in document order.

    A blockquote or table is one block (everything inside is absorbed). Each
    list item is a block, including nested ones, so every bullet can be
    commented on; paragraphs and code inside an item belong to the item.
    """
    blocks: List[Block] = []
    stack: List[str] = []  # open containers: quote | table | list_item
    for i, tok in enumerate(tokens):
        container = {"blockquote": "quote", "table": "table", "list_item": "list_item"}
        base = tok.type.rsplit("_", 1)[0]
        if tok.type.endswith("_close") and base in container:
            stack.pop()
            continue
        kind = _BLOCK_OPEN.get(tok.type) or _LEAF.get(tok.type)
        opens = tok.type.endswith("_open") and base in container
        absorbed = any(c in ("quote", "table") for c in stack) or (
            "list_item" in stack and kind != "list_item"
        )
        if opens:
            stack.append(container[base])
        if kind is None or absorbed:
            continue
        number = len(blocks) + 1
        tok.attrSet("id", f"p{number}")
        if tok.map:
            start, end = tok.map
            src = (start + 1 + line_offset, max(start + 1, end) + line_offset)
        else:
            src = (0, 0)
        if tok.nesting == 1:  # collect inline content up to the matching close
            depth, j = 0, i
            while j < len(tokens):
                depth += tokens[j].nesting
                if depth == 0:
                    break
                j += 1
            text = _plain(tokens[i : j + 1])
        else:
            text = _plain([tok])
        blocks.append(Block(id=f"p{number}", kind=kind, src_lines=src, text=text))
    return blocks


def _render_html(markdown: str, line_offset: int) -> Tuple[str, List[Block]]:
    md = MarkdownIt("commonmark", {"html": False}).enable("table")

    def image(_r, tokens, idx, _o, _e):
        return f"<em>[image: {escape(tokens[idx].content or 'image')}]</em>"

    md.add_render_rule("image", image)
    tokens = md.parse(markdown)
    blocks = _assign_blocks(tokens, line_offset)

    # fence/code_block/hr renderers would put the id on <code>/<hr>, where the
    # Story layout does not report it; wrap them in an identified <div> instead.
    for name in _LEAF:
        base = md.renderer.rules.get(name)
        if base is None:  # hr has no rule; the generic token renderer handles it

            def base(tokens, idx, options, env):
                return md.renderer.renderToken(tokens, idx, options, env)

        def wrapped(_self, tokens, idx, options, env, _base=base):
            block_id = tokens[idx].attrGet("id")
            if not block_id:
                return _base(tokens, idx, options, env)
            tokens[idx].attrs.pop("id", None)
            return f'<div id="{block_id}">{_base(tokens, idx, options, env)}</div>'

        md.add_render_rule(name, wrapped)
    return md.renderer.render(tokens, md.options, {}), blocks


def render_review_pdf(
    markdown: str,
    *,
    title: Optional[str] = None,
    subtitle: str = "",
    version: int = 1,
    note_margin: float = DEFAULT_NOTE_MARGIN,
    legend: bool = False,
    previous_digests: Optional[Sequence[str]] = None,
    responses: Optional[Sequence[dict]] = None,
) -> ReviewRender:
    """Render ``markdown`` (optionally with YAML front matter) for review.

    ``previous_digests``: block digests of the prior version; blocks not in it
    are flagged ``changed`` and get a bar in the left margin.
    ``responses``: [{"id", "status", "reply", "quote"?}] answering the previous
    round's comments, rendered as a closing page.
    """
    if not markdown or not markdown.strip():
        raise ValueError("Markdown content cannot be empty")
    fm, body, offset = split_front_matter(markdown)
    title = title or fm.get("title") or "Draft"

    html, blocks = _render_html(body, offset)
    if not any(b.kind == "heading" and b.number == 1 for b in blocks[:1]):
        html = f"<h1>{escape(title)}</h1>" + html
    page = pymupdf.Rect(0, 0, PAGE_W, PAGE_H)
    content = pymupdf.Rect(MARGIN_LEFT, MARGIN_TOP, PAGE_W - note_margin, PAGE_H - MARGIN_BOTTOM)
    starts: Dict[str, Tuple[int, float]] = {}

    def on_position(pos):
        if pos.id and pos.open_close & 1 and pos.id not in starts:
            starts[pos.id] = (pos.page_num, float(pos.rect[1]))

    story = pymupdf.Story(html=html, user_css=_CSS)
    doc = story.write_with_links(lambda _n, _f: (page, content, None), positionfn=on_position)
    try:
        prev = set(previous_digests or ())
        for b in blocks:
            b.page, b.y = starts.get(b.id, (0, 0.0))
            b.changed = bool(previous_digests is not None) and b.digest not in prev
        body_pages = len(doc)
        _decorate(doc, blocks, title, subtitle, version, content)
        if responses:
            _append_responses(doc, responses, version)
        if legend:
            _prepend_legend(doc, note_margin)
            for b in blocks:
                b.page += 1
        pdf = doc.tobytes(garbage=3, deflate=True)
        page_count = len(doc)
    finally:
        doc.close()
    del body_pages
    return ReviewRender(
        pdf=pdf,
        blocks=blocks,
        page_count=page_count,
        title=title,
        front_matter=fm,
        layout={
            "page_w": PAGE_W,
            "page_h": PAGE_H,
            "text_x0": content.x0,
            "text_x1": content.x1,
            "legend_pages": 1 if legend else 0,
        },
    )


def _decorate(doc, blocks: List[Block], title: str, subtitle: str, version: int, content) -> None:
    """Header, block numbers, change bars and page numbers."""
    total = len(doc)
    stamp = datetime.now().strftime("%d %b %Y")
    grey = (0.45, 0.45, 0.45)
    for index, page in enumerate(doc, start=1):
        header = f"{title} · v{version} · {stamp}"
        if subtitle:
            header += f" · {subtitle}"
        page.insert_text((MARGIN_LEFT, 22), header[:110], fontsize=6.5, fontname="helv", color=grey)
        page.insert_text(
            (PAGE_W - 40, PAGE_H - 14),
            f"{index}/{total}",
            fontsize=6.5,
            fontname="helv",
            color=grey,
        )
        page.draw_line(
            (content.x1 + 8, MARGIN_TOP),
            (content.x1 + 8, PAGE_H - MARGIN_BOTTOM),
            color=(0.85, 0.85, 0.85),
            width=0.4,
        )
    ordered = sorted((b for b in blocks if b.page), key=lambda b: (b.page, b.y))
    for n, b in enumerate(ordered):
        page = doc[b.page - 1]
        page.insert_text((6, b.y + 9), f"{b.number:>3}", fontsize=6, fontname="cour", color=grey)
        if b.changed:
            nxt = ordered[n + 1] if n + 1 < len(ordered) else None
            end_page = nxt.page if nxt else total
            end_y = nxt.y if nxt else PAGE_H - MARGIN_BOTTOM
            for pno in range(b.page, end_page + 1):
                y0 = b.y if pno == b.page else MARGIN_TOP
                y1 = end_y - 2 if pno == end_page else PAGE_H - MARGIN_BOTTOM
                if y1 - y0 > 2:
                    doc[pno - 1].draw_line((30, y0 + 2), (30, y1), color=(0, 0, 0), width=1.6)


def _append_responses(doc, responses: Sequence[dict], version: int) -> None:
    rows = []
    for r in responses:
        rows.append(
            "<tr><td>{}</td><td>{}</td><td>{}</td></tr>".format(
                escape(str(r.get("id", ""))),
                escape(str(r.get("status", ""))),
                escape(str(r.get("reply", "") or r.get("quote", ""))),
            )
        )
    html = (
        f"<h2>Responses to v{version - 1} review</h2>"
        "<table><tr><th>Comment</th><th>Status</th><th>What happened</th></tr>"
        + "".join(rows)
        + "</table>"
    )
    page = pymupdf.Rect(0, 0, PAGE_W, PAGE_H)
    content = pymupdf.Rect(MARGIN_LEFT, MARGIN_TOP, PAGE_W - 30, PAGE_H - MARGIN_BOTTOM)
    extra = pymupdf.Story(html=html, user_css=_CSS).write_with_links(
        lambda _n, _f: (page, content, None)
    )
    doc.insert_pdf(extra)
    extra.close()


LEGEND_MARKDOWN = """## How to mark this draft

- **Strike through** words to delete them. Write a replacement next to the strike to replace.
- **Scribble out** a passage to delete it entirely.
- **Underline** or **circle** words and write a note nearby to request a change.
- **Vertical bar** in the left margin flags whole lines; add a note in the right margin.
- **Handwriting in the right margin** is a comment on the paragraph beside it.
- Use `?` for "unclear", `!` for "important", `+` for "expand this".

The numbers in the left margin identify paragraphs. Changed paragraphs in later
versions carry a black bar in the left margin.

When you are done, move the document to the *Reviewed* folder.
"""


def _prepend_legend(doc, note_margin: float) -> None:
    fm, body, _ = split_front_matter(LEGEND_MARKDOWN)
    del fm
    html = MarkdownIt("commonmark").render(body)
    page = pymupdf.Rect(0, 0, PAGE_W, PAGE_H)
    content = pymupdf.Rect(MARGIN_LEFT, MARGIN_TOP, PAGE_W - note_margin, PAGE_H - MARGIN_BOTTOM)
    legend = pymupdf.Story(html=html, user_css=_CSS).write_with_links(
        lambda _n, _f: (page, content, None)
    )
    doc.insert_pdf(legend, from_page=0, to_page=0, start_at=0)
    legend.close()


def assign_words_to_blocks(
    blocks: Sequence[dict], page: int, words: Sequence[Tuple[float, float, float, float]]
) -> List[Optional[str]]:
    """Map word rects on ``page`` to block ids using block start positions.

    A word belongs to the last block starting at or before it in reading order
    (page, then y). This stays correct when a block breaks across pages, which
    PyMuPDF's element positions do not report.
    """
    starts = sorted(
        ((b["page"], b["y"], b["id"]) for b in blocks if b.get("page")), key=lambda s: s[:2]
    )
    out: List[Optional[str]] = []
    for w in words:
        key = (page, (w[1] + w[3]) / 2)
        owner = None
        for p, y, bid in starts:
            if (p, y - 1.0) <= key:
                owner = bid
            else:
                break
        out.append(owner)
    return out
