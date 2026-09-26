"""Tests for the workflow layer: review PDFs, ink analysis, review round-trips.

Ink is synthesised as real v6 ``.rm`` pages (via rmscene) at positions taken
from the rendered PDF's own word boxes, then packed into a document zip shaped
like a cloud download, so the whole pipeline runs without a tablet.
"""

import asyncio
import io
import json
import math
import uuid
import zipfile
from types import SimpleNamespace

import pymupdf
import pytest

from remarkable_mcp.workflows import handwriting, review_pdf
from remarkable_mcp.workflows.ink import load_document_ink_from_zip
from remarkable_mcp.workflows.marks import analyze_page
from remarkable_mcp.workflows.review import collect_requests, find_source_line
from remarkable_mcp.workflows.review_pdf import assign_words_to_blocks, render_review_pdf
from remarkable_mcp.workflows.state import Store, slugify

PPU = 0.3177  # points per device unit for the 1404x1872 grid

DRAFT = """---
title: 'Testing Pen Review'
slug: testing-pen-review
---

An agent can have permission to fetch data and still be poorly equipped to analyse it.
A month of readings is easy for an API to return.

Our harness deliberately has no shell. Giving the agent access to the production
database would introduce another problem entirely.

## The data path matters

The experiment was to put a small analytical database behind the existing API.
Each session gets its own in-memory workspace.

- First point about isolation
- Second point about capped results

Ending the session destroys the database and every intermediate table with it.
"""


# --------------------------------------------------------------------------- ink synthesis


def _rm_page(strokes):
    """Build v6 .rm bytes from strokes given as (points_pt, tool, page_width_pt)."""
    from rmscene import scene_items as si
    from rmscene.crdt_sequence import CrdtSequenceItem
    from rmscene.scene_stream import (
        AuthorIdsBlock,
        MigrationInfoBlock,
        PageInfoBlock,
        SceneLineItemBlock,
        SceneTreeBlock,
        TreeNodeBlock,
        write_blocks,
    )
    from rmscene.tagged_block_common import CrdtId

    layer = CrdtId(0, 11)
    blocks = [
        AuthorIdsBlock(author_uuids={1: uuid.uuid4()}),
        MigrationInfoBlock(migration_id=CrdtId(0, 1), is_device=True),
        PageInfoBlock(loads_count=1, merges_count=0, text_chars_count=0, text_lines_count=0),
        SceneTreeBlock(tree_id=layer, node_id=CrdtId(0, 0), is_update=True, parent_id=CrdtId(0, 0)),
        TreeNodeBlock(group=si.Group(node_id=layer)),
    ]
    for n, (points, tool, page_w) in enumerate(strokes):
        pts = [
            si.Point(
                x=(x - page_w / 2) / PPU, y=y / PPU, speed=0, direction=0, width=16, pressure=100
            )
            for x, y in points
        ]
        line = si.Line(
            color=si.PenColor.BLACK,
            tool=si.Pen(tool),
            points=pts,
            thickness_scale=1.0,
            starting_length=0.0,
        )
        blocks.append(
            SceneLineItemBlock(
                parent_id=layer,
                item=CrdtSequenceItem(
                    item_id=CrdtId(0, 20 + n),
                    left_id=CrdtId(0, 0),
                    right_id=CrdtId(0, 0),
                    deleted_length=0,
                    value=line,
                ),
            )
        )
    buf = io.BytesIO()
    write_blocks(buf, blocks)
    return buf.getvalue()


def _doc_zip(pdf_bytes, ink_by_page, doc_id=None):
    """A cloud-style document zip; ``ink_by_page`` maps 0-based page -> strokes."""
    doc_id = doc_id or str(uuid.uuid4())
    page_count = len(pymupdf.open(stream=pdf_bytes, filetype="pdf"))
    page_ids = [str(uuid.uuid4()) for _ in range(page_count)]
    content = {"fileType": "pdf", "formatVersion": 1, "pages": page_ids, "pageCount": page_count}
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr(f"{doc_id}.content", json.dumps(content))
        zf.writestr(f"{doc_id}.pdf", pdf_bytes)
        for index, strokes in ink_by_page.items():
            zf.writestr(f"{doc_id}/{page_ids[index]}.rm", _rm_page(strokes))
    return buf.getvalue()


FINELINER = 17
HIGHLIGHTER = 18


def _phrase_rects(pdf_bytes, phrase):
    """(page index, word rects) of the first occurrence of ``phrase``."""
    wanted = phrase.split()
    with pymupdf.open(stream=pdf_bytes, filetype="pdf") as doc:
        for pno, page in enumerate(doc):
            words = page.get_text("words")
            texts = [w[4].strip(".,:;") for w in words]
            for i in range(len(texts) - len(wanted) + 1):
                if texts[i : i + len(wanted)] == wanted:
                    hit = words[i : i + len(wanted)]
                    assert len({(w[5], w[6]) for w in hit}) == 1, f"'{phrase}' wraps; pick another"
                    return pno, [tuple(w[:4]) for w in hit]
    raise AssertionError(f"phrase not found: {phrase}")


def _hline(x0, x1, y, n=30):
    return [(x0 + (x1 - x0) * i / (n - 1), y + 0.4 * math.sin(i)) for i in range(n)]


def _strike(rects):
    x0, x1 = rects[0][0], rects[-1][2]
    y0, y1 = rects[0][1], rects[0][3]
    return _hline(x0, x1, y0 + 0.52 * (y1 - y0))


def _underline(rects):
    x0, x1 = rects[0][0], rects[-1][2]
    y0, y1 = rects[0][1], rects[0][3]
    return _hline(x0, x1, y0 + 0.86 * (y1 - y0))


def _ellipse(x0, y0, x1, y1, loops=1.1, n=80):
    cx, cy, rx, ry = (x0 + x1) / 2, (y0 + y1) / 2, (x1 - x0) / 2 + 6, (y1 - y0) / 2 + 4
    return [
        (
            cx + rx * math.cos(2 * math.pi * loops * i / n),
            cy + ry * math.sin(2 * math.pi * loops * i / n),
        )
        for i in range(n + 1)
    ]


def _scribble(x0, y0, x1, y1, passes=10):
    pts = []
    for p in range(passes):
        y = y0 + (y1 - y0) * p / (passes - 1)
        xs = (x0, x1) if p % 2 == 0 else (x1, x0)
        pts += [(xs[0], y), (xs[1], y + (y1 - y0) / passes / 2)]
    # densify
    out = []
    for a, b in zip(pts, pts[1:]):
        out += [(a[0] + (b[0] - a[0]) * t / 10, a[1] + (b[1] - a[1]) * t / 10) for t in range(10)]
    return out


def _handwriting(x, y, words=2, word_w=22.0, h=6.0):
    """Cursive-ish loops, one stroke per word."""
    strokes = []
    for w in range(words):
        start = x + w * (word_w + 7)
        pts = []
        for i in range(60):
            t = i / 59
            pts.append(
                (
                    start + t * word_w + 2.2 * math.cos(9 * t * math.pi),
                    y + h / 2 + (h / 2) * math.sin(9 * t * math.pi),
                )
            )
        strokes.append(pts)
    return strokes


def _vbar(x, y0, y1, n=20):
    return [(x + 0.3 * math.sin(i), y0 + (y1 - y0) * i / (n - 1)) for i in range(n)]


@pytest.fixture
def rendered():
    return render_review_pdf(DRAFT, legend=False)


def _page_w(pdf_bytes):
    with pymupdf.open(stream=pdf_bytes, filetype="pdf") as doc:
        return doc[0].rect.width


# --------------------------------------------------------------------------- review PDF


class TestReviewPdf:
    def test_blocks_map_to_source_lines(self, rendered):
        lines = DRAFT.splitlines()
        assert rendered.title == "Testing Pen Review"
        assert rendered.front_matter["slug"] == "testing-pen-review"
        first = rendered.blocks[0]
        assert first.kind == "paragraph"
        # front matter is 4 lines + blank; the paragraph starts on line 6
        assert first.src_lines == (6, 7)
        assert lines[first.src_lines[0] - 1].startswith("An agent can have permission")
        kinds = [b.kind for b in rendered.blocks]
        assert kinds == [
            "paragraph",
            "paragraph",
            "heading",
            "paragraph",
            "list_item",
            "list_item",
            "paragraph",
        ]
        heading = rendered.blocks[2]
        assert lines[heading.src_lines[0] - 1] == "## The data path matters"
        assert all(b.page >= 1 for b in rendered.blocks)
        assert len({b.id for b in rendered.blocks}) == len(rendered.blocks)

    def test_page_is_tablet_shaped_with_note_margin(self, rendered):
        with pymupdf.open(stream=rendered.pdf, filetype="pdf") as doc:
            rect = doc[0].rect
            assert rect.width == pytest.approx(review_pdf.PAGE_W)
            assert rect.height / rect.width == pytest.approx(4 / 3, rel=0.01)
            words = doc[0].get_text("words")
        body = [
            w
            for w in words
            if review_pdf.MARGIN_TOP < w[1] < review_pdf.PAGE_H - review_pdf.MARGIN_BOTTOM
            and w[0] > review_pdf.MARGIN_LEFT - 1
        ]
        assert max(w[2] for w in body) <= rendered.layout["text_x1"] + 1

    def test_margin_numbers_are_printed(self, rendered):
        with pymupdf.open(stream=rendered.pdf, filetype="pdf") as doc:
            margin = [w[4] for w in doc[0].get_text("words") if w[2] < review_pdf.MARGIN_LEFT - 4]
        assert {"1", "2", "3"} <= set(margin)

    def test_changed_blocks_against_previous_version(self, rendered):
        edited = DRAFT.replace("introduce another problem entirely", "cause trouble")
        v2 = render_review_pdf(
            edited, version=2, previous_digests=[b["digest"] for b in rendered.manifest_blocks()]
        )
        changed = [b.number for b in v2.blocks if b.changed]
        assert changed == [2]

    def test_legend_and_responses_pages(self, rendered):
        v2 = render_review_pdf(
            DRAFT,
            version=2,
            legend=True,
            responses=[{"id": "¶2 delete", "status": "done", "reply": "cut"}],
        )
        assert v2.page_count == rendered.page_count + 2
        assert v2.blocks[0].page == 2  # shifted by the legend page
        with pymupdf.open(stream=v2.pdf, filetype="pdf") as doc:
            assert "How to mark this draft" in doc[0].get_text()
            assert "Responses to v1 review" in doc[-1].get_text()

    def test_code_and_quote_blocks(self):
        md = (
            "Intro\n\n> quoted line one\n> quoted line two\n\n"
            "```python\nx = 1\ny = 2\n```\n\n---\n\nEnd\n"
        )
        r = render_review_pdf(md)
        assert [b.kind for b in r.blocks] == ["paragraph", "quote", "code", "hr", "paragraph"]
        assert r.blocks[2].src_lines == (6, 9)
        assert all(b.page for b in r.blocks)

    def test_empty_markdown_rejected(self):
        with pytest.raises(ValueError):
            render_review_pdf("   \n")


def test_words_assigned_across_page_breaks():
    long_para = " ".join(["lorem ipsum dolor"] * 400)
    r = render_review_pdf(f"Short intro.\n\n{long_para}\n\nAfter.\n")
    manifest = r.manifest_blocks()
    with pymupdf.open(stream=r.pdf, filetype="pdf") as doc:
        assert len(doc) >= 3
        words = [tuple(w[:4]) for w in doc[1].get_text("words") if w[1] > review_pdf.MARGIN_TOP]
    owners = assign_words_to_blocks(manifest, 2, words)
    assert set(owners) == {"p2"}


# --------------------------------------------------------------------------- mark analysis


def _analyse(rendered, strokes_by_page, seen=None):
    zip_bytes = _doc_zip(rendered.pdf, strokes_by_page)
    ink = load_document_ink_from_zip(zip_bytes)
    return collect_requests(ink, rendered.manifest_blocks(), DRAFT, seen)


class TestMarks:
    def test_strikethrough_maps_to_exact_source_line(self, rendered):
        pno, rects = _phrase_rects(rendered.pdf, "permission to fetch data")
        w = _page_w(rendered.pdf)
        [req] = _analyse(rendered, {pno: [(_strike(rects), FINELINER, w)]})
        assert req.mark.kind == "strikethrough"
        assert req.mark.intent == "delete"
        assert req.mark.target_text == "permission to fetch data"
        d = req.to_dict(None, "none")
        assert d["paragraph"] == 1
        assert d["src_lines"] == [6, 7]
        assert d["src_line"] == 6

    def test_underline_is_not_confused_with_next_line(self, rendered):
        pno, rects = _phrase_rects(rendered.pdf, "small analytical database")
        w = _page_w(rendered.pdf)
        [req] = _analyse(rendered, {pno: [(_underline(rects), FINELINER, w)]})
        assert req.mark.kind == "underline"
        assert req.mark.target_text == "small analytical database"
        assert req.to_dict(None, "none")["paragraph"] == 4

    def test_circle_with_margin_note_becomes_one_change_request(self, rendered):
        pno, rects = _phrase_rects(rendered.pdf, "Each session gets")
        w = _page_w(rendered.pdf)
        x0, y0 = rects[0][0], rects[0][1]
        x1, y1 = rects[-1][2], rects[-1][3]
        note_x = rendered.layout["text_x1"] + 14
        strokes = [(_ellipse(x0, y0, x1, y1), FINELINER, w)]
        strokes += [(s, FINELINER, w) for s in _handwriting(note_x, y0)]
        [req] = _analyse(rendered, {pno: strokes})
        assert req.mark.kind == "circle"
        assert req.mark.note is not None
        assert req.mark.intent == "change"
        assert req.mark.target_text == "Each session gets"

    def test_scribble_deletes_a_paragraph(self, rendered):
        pno, first = _phrase_rects(rendered.pdf, "Our harness deliberately")
        _, last = _phrase_rects(rendered.pdf, "introduce another problem")
        w = _page_w(rendered.pdf)
        box = (first[0][0], first[0][1], last[-1][2], last[-1][3])
        [req] = _analyse(rendered, {pno: [(_scribble(*box), FINELINER, w)]})
        assert req.mark.kind == "scribble"
        assert req.mark.intent == "delete"
        assert req.to_dict(None, "none")["paragraph"] == 2

    def test_margin_bar_flags_the_lines_beside_it(self, rendered):
        pno, first = _phrase_rects(rendered.pdf, "Ending the session")
        w = _page_w(rendered.pdf)
        bar = _vbar(first[0][0] - 8, first[0][1], first[0][3] + 12)
        [req] = _analyse(rendered, {pno: [(bar, FINELINER, w)]})
        assert req.mark.kind == "margin_bar"
        assert req.to_dict(None, "none")["paragraph"] == 7
        assert req.mark.target_text.startswith("Ending the session")

    def test_margin_note_alone_is_a_comment_on_the_paragraph_beside_it(self, rendered):
        pno, rects = _phrase_rects(rendered.pdf, "Our harness deliberately")
        w = _page_w(rendered.pdf)
        note_x = rendered.layout["text_x1"] + 14
        strokes = [(s, FINELINER, w) for s in _handwriting(note_x, rects[0][1] + 4, words=3)]
        [req] = _analyse(rendered, {pno: strokes})
        assert req.mark.kind == "note"
        assert req.mark.intent == "comment"
        assert req.to_dict(None, "not_transcribed")["paragraph"] == 2
        assert req.to_dict(None, "not_transcribed")["note_status"] == "not_transcribed"

    def test_highlighter_tool(self, rendered):
        pno, rects = _phrase_rects(rendered.pdf, "capped results")
        w = _page_w(rendered.pdf)
        y = (rects[0][1] + rects[0][3]) / 2
        [req] = _analyse(rendered, {pno: [(_hline(rects[0][0], rects[-1][2], y), HIGHLIGHTER, w)]})
        assert req.mark.kind == "highlight"
        assert req.mark.target_text == "capped results"

    def test_seen_marks_are_flagged(self, rendered):
        pno, rects = _phrase_rects(rendered.pdf, "permission to fetch data")
        w = _page_w(rendered.pdf)
        strokes = {pno: [(_strike(rects), FINELINER, w)]}
        [first] = _analyse(rendered, strokes)
        [again] = _analyse(rendered, strokes, seen=set(first.mark.seen_keys))
        assert first.new and not again.new
        assert first.mark.id == again.mark.id

    def test_page_without_manifest_uses_pdf_layout(self, rendered):
        pno, rects = _phrase_rects(rendered.pdf, "permission to fetch data")
        w = _page_w(rendered.pdf)
        ink = load_document_ink_from_zip(
            _doc_zip(rendered.pdf, {pno: [(_strike(rects), FINELINER, w)]})
        )
        [mark] = analyze_page(ink.annotated_pages()[0])
        assert mark.kind == "strikethrough"
        assert mark.block_ids and mark.block_ids[0].startswith("b")


def test_find_source_line():
    src = "one\ntwo words here\nthree more words\n"
    assert find_source_line(src, (1, 3), "more words") == 3
    assert find_source_line(src, (1, 3), "absent entirely") is None
    assert find_source_line(src, (1, 1), "words here") is None  # outside the block


def test_handwriting_crop_and_disabled_backend(monkeypatch, tmp_path):
    monkeypatch.setenv("REMARKABLE_WORKFLOW_STATE", str(tmp_path))
    monkeypatch.setenv("REMARKABLE_HANDWRITING_BACKEND", "none")
    from remarkable_mcp.workflows.ink import Stroke

    strokes = [
        Stroke(index=i, points=pts, tool="fineliner", color="black", width=1)
        for i, pts in enumerate(_handwriting(10, 10))
    ]
    png = handwriting.render_strokes_png(strokes)
    assert png.startswith(b"\x89PNG")
    assert handwriting.transcribe(png) == (None, "none")


def test_handwriting_backend_auto(monkeypatch):
    monkeypatch.delenv("REMARKABLE_HANDWRITING_BACKEND", raising=False)
    monkeypatch.delenv("GOOGLE_VISION_API_KEY", raising=False)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    assert handwriting.backend() == "none"
    monkeypatch.setenv("ANTHROPIC_API_KEY", "x")
    assert handwriting.backend() == "claude"
    monkeypatch.setenv("GOOGLE_VISION_API_KEY", "y")
    assert handwriting.backend() == "google"


def test_state_store_roundtrip(tmp_path):
    store = Store("reviews", root=tmp_path)
    store.put("a-b", {"slug": "a-b", "n": 1})
    assert store.get("a-b")["n"] == 1
    assert store.find(lambda r: r["n"] == 1)["slug"] == "a-b"
    with pytest.raises(ValueError):
        store.get("../escape")
    assert slugify("Ein Größerer Test: v2!") == "ein-groerer-test-v2"


# --------------------------------------------------------------------------- tools, end to end


class FakeCloud:
    """Just enough of RemarkableClient for the workflow tools."""

    def __init__(self):
        self.docs = {}
        self.zips = {}
        self.ink = {}  # doc id -> {page: strokes}, applied at download time

    def _doc(self, doc_id, name, parent, folder=False):
        doc = SimpleNamespace(
            id=doc_id,
            ID=doc_id,
            hash=uuid.uuid4().hex,
            VissibleName=name,
            name=name,
            parent=parent,
            Parent=parent,
            is_folder=folder,
            deleted=False,
            Type="CollectionType" if folder else "DocumentType",
            # Like the sync API: one entry per blob, stroke pages as <doc>/<page>.rm
            files=[{"id": f"{doc_id}.metadata", "hash": uuid.uuid4().hex}],
        )
        self.docs[doc_id] = doc
        return doc

    def get_meta_items(self):
        return list(self.docs.values())

    def create_folder(self, name, parent_id=""):
        return self._doc(str(uuid.uuid4()), name, parent_id, folder=True)

    def upload_document(self, content, name, file_type, parent_id="", orientation="portrait"):
        doc = self._doc(str(uuid.uuid4()), name, parent_id)
        self.zips[doc.id] = content
        # Like the sync client: the returned document has no file index loaded yet.
        return SimpleNamespace(**{**vars(doc), "files": []})

    def annotate(self, doc_id, strokes_by_page):
        self.ink[doc_id] = strokes_by_page
        doc = self.docs[doc_id]
        doc.hash = uuid.uuid4().hex
        doc.files = [f for f in doc.files if not f["id"].endswith(".rm")] + [
            {"id": f"{doc_id}/page{p}.rm", "hash": uuid.uuid4().hex} for p in strokes_by_page
        ]

    def touch(self, doc_id):
        """Tablet-side metadata change (e.g. opened): doc hash changes, strokes don't."""
        self.docs[doc_id].hash = uuid.uuid4().hex

    def download(self, doc):
        return _doc_zip(self.zips[doc.id], self.ink.get(doc.id, {}), doc_id=doc.id)


def _fake_path(item, by_id):
    parts = [item.VissibleName]
    parent = by_id.get(item.Parent)
    while parent is not None:
        parts.append(parent.VissibleName)
        parent = by_id.get(parent.Parent)
    return "/" + "/".join(reversed(parts))


@pytest.fixture
def cloud(monkeypatch, tmp_path):
    from remarkable_mcp.workflows import cloud as cloud_mod

    fake = FakeCloud()
    monkeypatch.setenv("REMARKABLE_WORKFLOW_STATE", str(tmp_path / "state"))
    monkeypatch.setenv("REMARKABLE_HANDWRITING_BACKEND", "none")
    monkeypatch.setattr(cloud_mod, "client", lambda: fake)
    monkeypatch.setattr(cloud_mod, "is_cloud", lambda: True)
    monkeypatch.setattr(cloud_mod, "refresh", lambda c: None)
    monkeypatch.setattr(cloud_mod, "get_item_path", _fake_path)
    return fake


def _json_of(result):
    if isinstance(result, list):
        result = result[0].text
    return json.loads(result)


def test_review_round_trip(cloud, tmp_path):
    from remarkable_mcp.workflows import tools

    draft = tmp_path / "post.md"
    draft.write_text(DRAFT)

    sent = _json_of(asyncio.run(tools.remarkable_review_send(source_path=str(draft))))
    assert sent["review"] == "testing-pen-review"
    assert sent["version"] == 1
    assert sent["document"] == "Testing Pen Review · v1"
    folder = next(d for d in cloud.docs.values() if d.is_folder)
    assert folder.VissibleName == "Review"

    listed = _json_of(asyncio.run(tools.remarkable_review_list()))
    assert listed["reviews"][0]["status"] == "waiting"

    doc_id = next(d.id for d in cloud.docs.values() if not d.is_folder)
    pdf = cloud.zips[doc_id]
    pno, rects = _phrase_rects(pdf, "permission to fetch data")
    w = _page_w(pdf)
    note_x = review_pdf.PAGE_W - review_pdf.DEFAULT_NOTE_MARGIN + 14
    _, other = _phrase_rects(pdf, "Our harness deliberately")
    cloud.annotate(
        doc_id,
        {
            pno: [(_strike(rects), FINELINER, w)]
            + [(s, FINELINER, w) for s in _handwriting(note_x, other[0][1] + 4)]
        },
    )
    assert (
        _json_of(asyncio.run(tools.remarkable_review_list()))["reviews"][0]["status"] == "annotated"
    )

    got = _json_of(asyncio.run(tools.remarkable_review_collect("testing-pen-review")))
    kinds = {r["kind"]: r for r in got["requests"]}
    assert set(kinds) == {"strikethrough", "note"}
    strike = kinds["strikethrough"]
    assert strike["src_line"] == 6 and strike["target"] == "permission to fetch data"
    assert kinds["note"]["paragraph"] == 2
    assert kinds["note"]["note_status"] == "not_transcribed"
    assert "include_images=true" in got["_hint"]

    # Collecting again returns nothing new; include_images returns crops.
    assert (
        _json_of(asyncio.run(tools.remarkable_review_collect("testing-pen-review")))["requests"]
        == []
    )
    with_images = asyncio.run(
        tools.remarkable_review_collect(
            "testing-pen-review", only_new=False, include_images=True, mark_seen=False
        )
    )
    assert isinstance(with_images, list)
    assert sum(1 for b in with_images if getattr(b, "type", "") == "image") == 2

    # Next version: responses page + changed paragraph bar.
    draft.write_text(DRAFT.replace("permission to fetch data", "permission to read data"))
    v2 = _json_of(
        asyncio.run(
            tools.remarkable_review_send(
                source_path=str(draft),
                responses=[{"id": strike["id"], "status": "done", "reply": "reworded"}],
            )
        )
    )
    assert v2["version"] == 2
    assert v2["changed_paragraphs"] == [1]
    v2_doc = next(d for d in cloud.docs.values() if d.VissibleName.endswith("v2"))
    with pymupdf.open(stream=cloud.zips[v2_doc.id], filetype="pdf") as doc:
        last = doc[-1].get_text()
    assert "Responses to v1 review" in last and "reworded" in last


def test_review_send_requires_one_source(cloud):
    from remarkable_mcp.workflows import tools

    err = _json_of(asyncio.run(tools.remarkable_review_send()))
    assert err["_error"]["type"] == "invalid_arguments"


def test_review_collect_unknown_review(cloud):
    from remarkable_mcp.workflows import tools

    err = _json_of(asyncio.run(tools.remarkable_review_collect("nope")))
    assert err["_error"]["type"] == "review_not_found"


def test_review_done_folder_status(cloud, tmp_path):
    from remarkable_mcp.workflows import tools

    asyncio.run(tools.remarkable_review_send(markdown="# Title\n\nBody text here.\n"))
    doc = next(d for d in cloud.docs.values() if not d.is_folder)
    review_folder = next(d for d in cloud.docs.values() if d.is_folder)
    done = cloud.create_folder("Reviewed", review_folder.id)
    doc.parent = doc.Parent = done.id
    # Moved without new ink: nothing to collect.
    listed = _json_of(asyncio.run(tools.remarkable_review_list()))
    assert listed["reviews"][0]["status"] == "collected"
    cloud.annotate(doc.id, {0: [(_hline(60, 200, 100), FINELINER, 446.0)]})
    listed = _json_of(asyncio.run(tools.remarkable_review_list()))
    assert listed["reviews"][0]["status"] == "done"
    asyncio.run(tools.remarkable_review_collect(listed["reviews"][0]["review"]))
    listed = _json_of(asyncio.run(tools.remarkable_review_list()))
    assert listed["reviews"][0]["status"] == "collected"


def test_annotations_tool_on_any_document(cloud, monkeypatch):
    from remarkable_mcp.workflows import tools

    r = render_review_pdf(DRAFT)
    doc = cloud.upload_document(r.pdf, "Some PDF", "pdf")
    pno, rects = _phrase_rects(r.pdf, "permission to fetch data")
    cloud.annotate(doc.id, {pno: [(_strike(rects), FINELINER, _page_w(r.pdf))]})
    monkeypatch.setattr(
        "remarkable_mcp.tools._find_target_document",
        lambda collection, by_id, name: next(
            (d for d in collection if d.VissibleName == name), None
        ),
    )
    got = _json_of(asyncio.run(tools.remarkable_annotations("Some PDF")))
    assert got["pages_with_ink"] == [pno + 1]
    [mark] = got["marks"]
    assert mark["kind"] == "strikethrough" and mark["target"] == "permission to fetch data"


# --------------------------------------------------------------------------- regressions


def _doc_zip_v2(pdf_bytes, ink_by_page, extra_first_page=False):
    """formatVersion 2 (cPages) zip, optionally with a user-inserted blank first page."""
    doc_id = str(uuid.uuid4())
    page_count = len(pymupdf.open(stream=pdf_bytes, filetype="pdf"))
    entries = [{"id": str(uuid.uuid4()), "redir": {"value": i}} for i in range(page_count)]
    if extra_first_page:
        entries.insert(0, {"id": str(uuid.uuid4())})
    content = {"fileType": "pdf", "formatVersion": 2, "cPages": {"pages": entries}}
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr(f"{doc_id}.content", json.dumps(content))
        zf.writestr(f"{doc_id}.pdf", pdf_bytes)
        for pdf_index, strokes in ink_by_page.items():
            page_id = next(e["id"] for e in entries if e.get("redir", {}).get("value") == pdf_index)
            zf.writestr(f"{doc_id}/{page_id}.rm", _rm_page(strokes))
    return buf.getvalue()


def test_note_added_after_collect_comes_back(rendered):
    pno, rects = _phrase_rects(rendered.pdf, "permission to fetch data")
    w = _page_w(rendered.pdf)
    strike = [(_strike(rects), FINELINER, w)]
    [first] = _analyse(rendered, {pno: strike})
    seen = set(first.mark.seen_keys)
    note = [(s, FINELINER, w) for s in _handwriting(rects[-1][2] + 30, rects[0][1] - 2, words=1)]
    [later] = _analyse(rendered, {pno: strike + note}, seen=seen)
    assert later.mark.id == first.mark.id  # same mark ...
    assert later.mark.intent == "replace"  # ... now with a replacement note
    assert later.new  # and therefore delivered again


def test_inserted_tablet_page_keeps_mark_ids(rendered):
    pno, rects = _phrase_rects(rendered.pdf, "permission to fetch data")
    ink_pages = {pno: [(_strike(rects), FINELINER, _page_w(rendered.pdf))]}
    plain = load_document_ink_from_zip(_doc_zip_v2(rendered.pdf, ink_pages))
    shifted = load_document_ink_from_zip(
        _doc_zip_v2(rendered.pdf, ink_pages, extra_first_page=True)
    )
    [a] = collect_requests(plain, rendered.manifest_blocks(), DRAFT)
    [b] = collect_requests(shifted, rendered.manifest_blocks(), DRAFT)
    assert a.page + 1 == b.page
    assert a.mark.id == b.mark.id and a.mark.seen_keys == b.mark.seen_keys
    assert b.to_dict(None, "none")["paragraph"] == 1


def test_rotated_pdf_page_words_align_with_ink():
    doc = pymupdf.open()
    page = doc.new_page(width=300, height=500)
    page.insert_text((40, 60), "rotate me please", fontsize=14)
    page.set_rotation(90)
    pdf = doc.tobytes()
    shown = page.rect  # displayed size: 500 x 300
    with pymupdf.open(stream=pdf, filetype="pdf") as d:
        raw = [w for w in d[0].get_text("words") if w[4] == "me"][0]
        box = (pymupdf.Rect(raw[:4]) * d[0].rotation_matrix).normalize()
    w = shown.width
    # A strike through "me" in displayed coordinates.
    strike = [(box.x0 + (box.x1 - box.x0) * t / 20, (box.y0 + box.y1) / 2) for t in range(21)]
    if box.width < box.height:  # vertical text after rotation: strike vertically
        strike = [((box.x0 + box.x1) / 2, box.y0 + (box.y1 - box.y0) * t / 20) for t in range(21)]
    ink = load_document_ink_from_zip(_doc_zip(pdf, {0: [(strike, FINELINER, w)]}))
    pg = ink.pages[0]
    assert (pg.width, pg.height) == (shown.width, shown.height)
    me = next(x for x in pg.words if x.text == "me")
    assert me.rect == pytest.approx(tuple(box), abs=0.5)


def test_ink_on_responses_page_is_not_a_paragraph_request():
    r = render_review_pdf(
        DRAFT, version=2, responses=[{"id": "x", "status": "done", "reply": "ok"}]
    )
    last = r.page_count - 1
    ink = load_document_ink_from_zip(
        _doc_zip(r.pdf, {last: [(s, FINELINER, 446.0) for s in _handwriting(80, 200)]})
    )
    [req] = collect_requests(ink, r.manifest_blocks(), DRAFT, layout=r.layout)
    assert req.block is None
    assert "paragraph" not in req.to_dict(None, "none")


def test_bar_beside_paragraph_numbers_targets_text_not_numbers(rendered):
    pno, first = _phrase_rects(rendered.pdf, "Ending the session")
    bar = _vbar(26, first[0][1], first[0][3] + 12)  # where change bars live
    zip_bytes = _doc_zip(rendered.pdf, {pno: [(bar, FINELINER, _page_w(rendered.pdf))]})
    ink = load_document_ink_from_zip(zip_bytes)
    [req] = collect_requests(ink, rendered.manifest_blocks(), DRAFT, layout=rendered.layout)
    assert req.mark.kind == "margin_bar"
    assert req.mark.target_text.startswith("Ending the session")


def test_nested_list_items_have_own_text():
    r = render_review_pdf("- parent item\n  continued\n  - child item\n")
    texts = [b.text for b in r.blocks]
    assert texts == ["parent item continued", "child item"]


def test_trashed_review_folder_is_not_reused(cloud):
    from remarkable_mcp.workflows import tools

    trashed = cloud.create_folder("Review", "trash")
    asyncio.run(tools.remarkable_review_send(markdown="# T\n\nBody.\n"))
    doc = next(d for d in cloud.docs.values() if not d.is_folder)
    assert doc.Parent != trashed.id
    doc.parent = doc.Parent = "trash"
    listed = _json_of(asyncio.run(tools.remarkable_review_list()))
    assert listed["reviews"][0]["status"] == "missing"


def test_review_ids_never_collide(cloud):
    from remarkable_mcp.workflows import tools

    ids = {
        _json_of(asyncio.run(tools.remarkable_review_send(markdown="# Same\n\nText.\n")))["review"]
        for _ in range(3)
    }
    assert len(ids) == 3
