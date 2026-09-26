"""Robustness: every analyser survives random and degenerate ink, within a time budget.

Seeded pseudo-random pages (reproducible) mix handwriting-like squiggles,
long lines, loops, dots, zero-size strokes and off-page coordinates.
"""

import math
import random
import time

import pymupdf
import pytest

from remarkable_mcp.workflows.code_review import collect_comments, parse_diff, render_diff
from remarkable_mcp.workflows.forms import read_answers, render_form, render_triage, stray_strokes
from remarkable_mcp.workflows.inbox import segment_entries
from remarkable_mcp.workflows.ink import PageInk, Stroke, Word
from remarkable_mcp.workflows.marks import analyze_page
from remarkable_mcp.workflows.review import collect_requests
from remarkable_mcp.workflows.review_pdf import render_review_pdf
from remarkable_mcp.workflows.sketch import recognise, to_mermaid, to_svg
from remarkable_mcp.workflows.table import find_table
from remarkable_mcp.workflows.wireframe import build_wireframe, to_html

W, H = 446.0, 595.0


def _stroke(rng, i):
    kind = rng.choice(["squiggle", "line", "loop", "dot", "zero", "offpage", "scribble"])
    x, y = rng.uniform(0, W), rng.uniform(0, H)
    if kind == "dot":
        pts = [(x, y)]
    elif kind == "zero":
        pts = [(x, y)] * rng.randint(2, 20)
    elif kind == "offpage":
        pts = [
            (x + rng.uniform(-2000, 2000), y + rng.uniform(-2000, 2000))
            for _ in range(rng.randint(2, 30))
        ]
    elif kind == "line":
        a = rng.uniform(0, math.pi)
        L = rng.uniform(5, 400)
        pts = [(x + L * t / 20 * math.cos(a), y + L * t / 20 * math.sin(a)) for t in range(21)]
    elif kind == "loop":
        r1, r2 = rng.uniform(3, 150), rng.uniform(3, 100)
        pts = [
            (x + r1 * math.cos(t / 10), y + r2 * math.sin(t / 10))
            for t in range(rng.randint(10, 80))
        ]
    elif kind == "scribble":
        pts = [
            (x + rng.uniform(-60, 60), y + rng.uniform(-20, 20)) for _ in range(rng.randint(5, 200))
        ]
    else:
        pts = [(x + t * 1.5, y + 4 * math.sin(t)) for t in range(rng.randint(3, 60))]
    tool = rng.choice(["fineliner", "fineliner", "ballpoint", "highlighter"])
    return Stroke(index=i, points=pts, tool=tool, color="black", width=1.0)


def _words(rng, n):
    out = []
    for i in range(n):
        x, y = rng.uniform(20, 380), rng.uniform(20, 560)
        w, h = rng.uniform(5, 60), rng.uniform(6, 14)
        out.append(Word(text=f"w{i}", rect=(x, y, x + w, y + h), block=i // 12, line=i // 6))
    return out


def _page(seed, strokes=150, words=120, pdf=True):
    rng = random.Random(seed)
    return PageInk(
        page=1,
        pdf_page=0 if pdf else None,
        width=W,
        height=H,
        strokes=[_stroke(rng, i) for i in range(strokes)],
        words=_words(rng, words) if pdf else [],
        page_id=f"p{seed}",
    )


SEEDS = list(range(12))


@pytest.mark.parametrize("seed", SEEDS)
def test_marks_survive_random_pages(seed):
    page = _page(seed)
    marks = analyze_page(page)
    for m in marks:
        assert m.kind and m.id.startswith("m")
        assert all(math.isfinite(v) for v in m.rect)
    analyze_page(_page(seed, pdf=False))  # notebook page: no words at all


@pytest.mark.parametrize("seed", SEEDS)
def test_sketch_table_wireframe_inbox_survive(seed):
    page = _page(seed, words=0, pdf=False)
    d = recognise(page.strokes)
    to_mermaid(d)
    svg = to_svg(d)
    assert svg.startswith("<svg")
    find_table(page.strokes)
    to_html(build_wireframe(page.strokes))
    segment_entries(page)


@pytest.mark.parametrize(
    "strokes",
    [
        [],
        [Stroke(0, [(10, 10)], "fineliner", "black", 1)],
        [Stroke(0, [(10, 10)] * 50, "fineliner", "black", 1)],
        [Stroke(0, [(0, 0), (0, 0)], "highlighter", "yellow", 1)],
        [Stroke(i, [(1e6, -1e6), (1e6 + 1, -1e6)], "fineliner", "black", 1) for i in range(5)],
    ],
    ids=["empty", "dot", "zero-length", "zero-highlighter", "far-off-page"],
)
def test_degenerate_strokes(strokes):
    page = PageInk(1, 0, W, H, strokes=strokes, words=_words(random.Random(1), 30))
    analyze_page(page)
    recognise(strokes)
    find_table(strokes)
    build_wireframe(strokes)
    segment_entries(page)


def test_dense_pages_stay_fast():
    budgets = {
        "marks": (lambda p: analyze_page(p), 6.0),
        "sketch": (lambda p: recognise(p.strokes), 3.0),
        "table": (lambda p: find_table(p.strokes), 2.0),
        "inbox": (lambda p: segment_entries(p), 2.0),
    }
    page = _page(99, strokes=2000, words=400)
    for name, (fn, budget) in budgets.items():
        t0 = time.time()
        fn(page)
        elapsed = time.time() - t0
        assert elapsed < budget, f"{name} took {elapsed:.1f}s on a 2000-stroke page"


def test_forms_and_reviews_survive_random_ink():
    form = render_form(
        "F",
        [
            {"id": "c", "type": "choice", "label": "Pick", "options": ["a", "b", "c"]},
            {"id": "m", "type": "multi", "label": "Many", "options": ["x", "y"]},
            {"id": "s", "type": "scale", "label": "Scale"},
            {"id": "k", "type": "checkbox", "label": "Box"},
            {"id": "t", "type": "text", "label": "Text", "lines": 2},
        ],
    )
    triage = render_triage(
        "T", [{"id": f"i{n}", "title": f"Item {n}"} for n in range(8)], ["A", "B"]
    )
    review = render_review_pdf("# T\n\n" + "Some words here. " * 200 + "\n\n- a\n- b\n")
    diff_render = render_diff(
        "PR", parse_diff("--- a/x\n+++ b/x\n@@ -1,2 +1,2 @@\n-old\n+new\n same\n")
    )
    for seed in SEEDS[:6]:
        page = _page(seed)
        pages = {1: page}
        read_answers(form.manifest(), pages)
        stray_strokes(form.manifest(), pages)
        read_answers(triage.manifest(), pages)
        collect_comments(pages, diff_render.rows)
        from remarkable_mcp.workflows.ink import DocumentInk

        ink = DocumentInk(pages=[page], pdf_bytes=review.pdf, page_count=review.page_count)
        collect_requests(ink, review.manifest_blocks(), "# T\n", set(), review.layout)


@pytest.mark.parametrize(
    "markdown",
    [
        "x",
        "# Only a heading",
        "```\ncode only\n```",
        "| a | b |\n|---|---|\n| 1 | 2 |",
        "> quote\n> > nested quote",
        "- a\n  - b\n    - c\n      - d",
        "1. one\n2. two\n\n   para in item",
        "---\ntitle: fm only\n---\n\ntext",
        "<script>alert(1)</script>\n\n<b>raw html</b>",
        "Line with emoji \U0001f600 and umlauts äöü and CJK 漢字",
        "word " * 5000,
    ],
    ids=[
        "tiny",
        "heading",
        "code",
        "table",
        "nested-quote",
        "deep-list",
        "ordered",
        "frontmatter",
        "raw-html",
        "unicode",
        "huge",
    ],
)
def test_review_pdf_renders_any_markdown(markdown):
    r = render_review_pdf(markdown)
    with pymupdf.open(stream=r.pdf, filetype="pdf") as doc:
        assert len(doc) == r.page_count >= 1
        text = "".join(p.get_text() for p in doc)
    assert "alert(1)" not in text or "<script>" in text  # raw HTML is escaped, never executed
    for b in r.blocks:
        assert b.page >= 1 and b.src_lines[0] <= b.src_lines[1]
