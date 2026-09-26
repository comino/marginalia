"""Metamorphic tests: transformations that must not change what a mark means.

Built on real rendered review pages (real word boxes) with synthetic marks.
"""

import math
import random
from dataclasses import replace

import pytest

from remarkable_mcp.workflows.ink import Stroke, load_document_ink_from_zip
from remarkable_mcp.workflows.marks import analyze_page
from remarkable_mcp.workflows.review_pdf import render_review_pdf
from test_workflows import (
    DRAFT,
    _doc_zip,
    _ellipse,
    _handwriting,
    _phrase_rects,
    _scribble,
    _strike,
    _underline,
    _vbar,
)


@pytest.fixture(scope="module")
def base():
    r = render_review_pdf(DRAFT)
    page = load_document_ink_from_zip(_doc_zip(r.pdf, {})).pages[0]
    rects = {
        name: _phrase_rects(r.pdf, phrase)[1]
        for name, phrase in {
            "strike": "permission to fetch data",
            "under": "small analytical database",
            "circle": "Each session gets",
            "scrib1": "Our harness deliberately",
            "scrib2": "introduce another problem",
            "bar": "Ending the session",
        }.items()
    }
    return page, rects


def _marks(page, rects):
    s1, s2 = rects["scrib1"], rects["scrib2"]
    b = rects["bar"]
    c = rects["circle"]
    strokes = [
        _strike(rects["strike"]),
        _underline(rects["under"]),
        _ellipse(c[0][0], c[0][1], c[-1][2], c[-1][3]),
        _scribble(s1[0][0], s1[0][1], s2[-1][2], s2[-1][3]),
        _vbar(b[0][0] - 8, b[0][1], b[0][3] + 12),
    ]
    strokes += _handwriting(345, c[0][1], words=2)  # margin note beside the circle
    return [Stroke(i, pts, "fineliner", "black", 1.0) for i, pts in enumerate(strokes)]


def _signature(page):
    return sorted((m.kind, m.target_text, m.note is not None) for m in analyze_page(page))


def _with(page, strokes=None, words=None):
    return replace(
        page,
        strokes=strokes if strokes is not None else page.strokes,
        words=words if words is not None else page.words,
    )


@pytest.fixture(scope="module")
def reference(base):
    page, rects = base
    marked = _with(page, strokes=_marks(page, rects))
    sig = _signature(marked)
    kinds = [k for k, _, _ in sig]
    assert sorted(kinds) == sorted(
        ["strikethrough", "underline", "circle", "scribble", "margin_bar"]
    )
    return marked, sig


def _map_strokes(page, fn):
    return [replace(s, points=fn(s.points)) for s in page.strokes]


def test_translation_of_the_whole_page(reference):
    page, sig = reference
    dx, dy = 13.7, -21.3
    moved = _with(
        page,
        strokes=_map_strokes(page, lambda pts: [(x + dx, y + dy) for x, y in pts]),
        words=[
            replace(w, rect=(w.rect[0] + dx, w.rect[1] + dy, w.rect[2] + dx, w.rect[3] + dy))
            for w in page.words
        ],
    )
    assert _signature(moved) == sig


def test_stroke_direction_does_not_matter(reference):
    page, sig = reference
    assert _signature(_with(page, strokes=_map_strokes(page, lambda pts: pts[::-1]))) == sig


@pytest.mark.parametrize("factor", [2, 0.5])
def test_sampling_density_does_not_matter(reference, factor):
    page, sig = reference

    def resample(pts):
        if len(pts) < 3:
            return pts
        if factor == 2:
            out = []
            for a, b in zip(pts, pts[1:]):
                out += [a, ((a[0] + b[0]) / 2, (a[1] + b[1]) / 2)]
            return out + [pts[-1]]
        return pts[::2] + ([pts[-1]] if len(pts) % 2 == 0 else [])

    assert _signature(_with(page, strokes=_map_strokes(page, resample))) == sig


@pytest.mark.parametrize("seed", range(5))
def test_small_tremor_does_not_matter(reference, seed):
    page, sig = reference
    rng = random.Random(seed)
    shaky = _map_strokes(
        page, lambda pts: [(x + rng.uniform(-0.4, 0.4), y + rng.uniform(-0.4, 0.4)) for x, y in pts]
    )
    assert _signature(_with(page, strokes=shaky)) == sig


def test_order_of_strokes_does_not_matter(reference):
    page, sig = reference
    rng = random.Random(3)
    shuffled = page.strokes[:]
    rng.shuffle(shuffled)
    shuffled = [replace(s, index=i) for i, s in enumerate(shuffled)]
    assert _signature(_with(page, strokes=shuffled)) == sig


def test_unrelated_ink_elsewhere_does_not_change_marks(reference):
    page, sig = reference
    doodle = Stroke(
        99,
        [(400 + 5 * math.cos(t / 3), 560 + 5 * math.sin(t / 3)) for t in range(20)],
        "fineliner",
        "black",
        1.0,
    )
    extra = _signature(_with(page, strokes=page.strokes + [doodle]))
    assert [s for s in extra if s[0] != "note"] == [s for s in sig if s[0] != "note"]
