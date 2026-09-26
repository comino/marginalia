"""Regression tests for the adversarial classifier review.

Each test reproduces a realistic human variation that used to be misread,
over several deterministic variants. Words come from a real rendered page
(CharisSIL 10 pt: word box 14.7 pt, baseline at 0.818 of the box).
"""

import math
import random
from dataclasses import replace

import pytest

from remarkable_mcp.workflows.forms import read_answers, render_form
from remarkable_mcp.workflows.inbox import segment_entries
from remarkable_mcp.workflows.ink import PageInk, Stroke, load_document_ink_from_zip
from remarkable_mcp.workflows.marks import analyze_page
from remarkable_mcp.workflows.review_pdf import render_review_pdf
from remarkable_mcp.workflows.sketch import classify_outline, recognise
from test_workflows import DRAFT, _doc_zip, _phrase_rects

RNG = random.Random(7)


@pytest.fixture(scope="module")
def page():
    r = render_review_pdf(DRAFT)
    return r, load_document_ink_from_zip(_doc_zip(r.pdf, {})).pages[0]


def S(pts, i=0, tool="fineliner"):
    return Stroke(i, list(pts), tool, "black", 1.0)


def line(x0, x1, y, n=40, wave=0.0, period=12.0, slope=0.0, jitter=0.0, rng=None):
    rng = rng or RNG
    return [
        (
            x0 + (x1 - x0) * t / (n - 1),
            y
            + wave * math.sin((x1 - x0) * t / (n - 1) / period * 2 * math.pi)
            + slope * (x1 - x0) * t / (n - 1)
            + rng.uniform(-jitter, jitter),
        )
        for t in range(n)
    ]


def kinds(pg, strokes):
    return [(m.kind, m.target_text) for m in analyze_page(replace(pg, strokes=strokes))]


def words_of(r, phrase):
    return _phrase_rects(r.pdf, phrase)[1]


def baseline_y(pg, rect):
    w = next(
        w for w in pg.words if abs(w.rect[0] - rect[0]) < 0.5 and abs(w.rect[1] - rect[1]) < 0.5
    )
    return w.baseline, w.size


# 7: strike through the lower half of the x-height is a strike, not an underline
@pytest.mark.parametrize("above", [0.14, 0.2, 0.25, 0.32, 0.38, 0.45])
def test_strike_low_in_the_x_height(page, above):
    r, pg = page
    rects = words_of(r, "permission to fetch data")
    base, size = baseline_y(pg, rects[0])
    y = base - above * size  # above the baseline, inside the x-height
    [(kind, target)] = kinds(pg, [S(line(rects[0][0], rects[-1][2], y))])
    assert kind == "strikethrough" and target == "permission to fetch data"


@pytest.mark.parametrize("below", [-0.03, 0.0, 0.05, 0.15, 0.3])
def test_underline_touching_or_below_the_baseline(page, below):
    r, pg = page
    rects = words_of(r, "permission to fetch data")
    base, size = baseline_y(pg, rects[0])
    [(kind, target)] = kinds(pg, [S(line(rects[0][0], rects[-1][2], base + below * size))])
    assert kind == "underline" and target == "permission to fetch data"


# 2: overshooting a strike must not swallow the neighbouring short word
@pytest.mark.parametrize("over", [3, 5, 7, 9])
def test_strike_overshoot_keeps_to_the_phrase(page, over):
    r, pg = page
    rects = words_of(r, "have permission to fetch")
    base, size = baseline_y(pg, rects[0])
    strike = line(rects[1][0] - over, rects[-1][2] + over, base - 0.28 * size)
    [(kind, target)] = kinds(pg, [S(strike)])
    assert kind == "strikethrough" and target == "permission to fetch"


# 6: strikes and underlines on short words
@pytest.mark.parametrize("word", ["to", "is", "an", "API."])
@pytest.mark.parametrize("which", ["strikethrough", "underline"])
def test_marks_on_short_words(page, word, which):
    r, pg = page
    rect = next(w.rect for w in pg.words if w.text == word)
    base, size = baseline_y(pg, rect)
    y = base - 0.28 * size if which == "strikethrough" else base + 0.1 * size
    [(kind, target)] = kinds(pg, [S(line(rect[0] - 1, rect[2] + 1, y, n=12))])
    assert kind == which and target == word


# 9: wavy strikes / underlines
@pytest.mark.parametrize("wave", [0.8, 1.2, 1.6])
def test_wavy_lines(page, wave):
    r, pg = page
    rects = words_of(r, "permission to fetch data")
    base, size = baseline_y(pg, rects[0])
    strike = line(rects[0][0], rects[-1][2], base - 0.28 * size, n=80, wave=wave, period=9)
    under = line(rects[0][0], rects[-1][2], base + 0.15 * size, n=80, wave=wave, period=9)
    assert kinds(pg, [S(strike)])[0][0] == "strikethrough"
    assert kinds(pg, [S(under)])[0][0] == "underline"


# 12: a strike drawn in two pulls is one mark
def test_strike_in_two_pulls_is_one_mark(page):
    r, pg = page
    rects = words_of(r, "permission to fetch data")
    base, size = baseline_y(pg, rects[0])
    mid = (rects[1][0] + rects[1][2]) / 2
    a = line(rects[0][0], mid - 1, base - 0.28 * size)
    b = line(mid + 1, rects[-1][2], base - 0.3 * size)
    assert kinds(pg, [S(a, 0), S(b, 1)]) == [("strikethrough", "permission to fetch data")]


# 5: scribbles in several strokes, vertical zigzags, single words
def zigzag(x0, y0, x1, y1, passes, vertical=False):
    pts = []
    for p in range(passes + 1):
        t = p / passes
        if vertical:
            x = x0 + (x1 - x0) * t
            pts.append((x, y0 if p % 2 == 0 else y1))
        else:
            y = y0 + (y1 - y0) * t
            pts.append((x0 if p % 2 == 0 else x1, y))
    dense = []
    for a, b in zip(pts, pts[1:]):
        dense += [(a[0] + (b[0] - a[0]) * k / 8, a[1] + (b[1] - a[1]) * k / 8) for k in range(8)]
    return dense + [pts[-1]]


@pytest.mark.parametrize("pieces", [2, 3, 6])
def test_scribble_in_several_strokes(page, pieces):
    r, pg = page
    first = words_of(r, "Our harness deliberately")
    x0, y0, x1 = first[0][0], first[0][1], first[-1][2]
    y1 = first[0][3]
    step = (x1 - x0) / pieces
    strokes = [S(zigzag(x0 + k * step, y0, x0 + (k + 1) * step, y1, 7), k) for k in range(pieces)]
    [(kind, target)] = kinds(pg, strokes)
    assert kind == "scribble" and "harness" in target


def test_vertical_zigzag_and_single_word_scribble(page):
    r, pg = page
    rects = words_of(r, "permission to fetch data")
    x0, y0, x1, y1 = rects[0][0], rects[0][1] + 2, rects[-1][2], rects[0][3] - 2
    assert kinds(pg, [S(zigzag(x0, y0, x1, y1, 14, vertical=True))])[0][0] == "scribble"
    w = rects[0]
    assert (
        kinds(pg, [S(zigzag(w[0], w[1] + 2, w[2], w[3] - 2, 8, vertical=True))])[0][0] == "scribble"
    )


# 3: dense handwriting between lines is a note, never a deletion
@pytest.mark.parametrize("seed", range(6))
def test_writing_between_lines_is_not_a_scribble(page, seed):
    r, pg = page
    rects = words_of(r, "Our harness deliberately")
    rng = random.Random(seed)
    top = rects[0][1]
    pts = [
        (rects[0][0] + 1.2 * t + rng.uniform(-0.3, 0.3), top - 5 + 4.5 + 4.5 * math.sin(t * 1.9))
        for t in range(40)
    ]
    assert "scribble" not in [k for k, _ in kinds(pg, [S(pts)])]


# 8: circles drawn in two arcs, or not quite closed
def arc(cx, cy, rx, ry, a0, a1, n=40):
    return [
        (
            cx + rx * math.cos(a0 + (a1 - a0) * t / (n - 1)),
            cy + ry * math.sin(a0 + (a1 - a0) * t / (n - 1)),
        )
        for t in range(n)
    ]


@pytest.mark.parametrize("sweep", [0.8, 0.9, 1.0, 1.08])
def test_circles_that_do_not_quite_close(page, sweep):
    r, pg = page
    rects = words_of(r, "Each session gets")
    x0, y0, x1, y1 = rects[0][0], rects[0][1], rects[-1][2], rects[-1][3]
    cx, cy, rx, ry = (x0 + x1) / 2, (y0 + y1) / 2, (x1 - x0) / 2 + 6, (y1 - y0) / 2 + 4
    [(kind, target)] = kinds(pg, [S(arc(cx, cy, rx, ry, 0.3, 0.3 + 2 * math.pi * sweep, 70))])
    assert kind == "circle" and target == "Each session gets"


@pytest.mark.parametrize("start", [0.0, 0.7, 1.9, 3.1])
def test_circle_drawn_as_two_arcs(page, start):
    r, pg = page
    rects = words_of(r, "Each session gets")
    x0, y0, x1, y1 = rects[0][0], rects[0][1], rects[-1][2], rects[-1][3]
    cx, cy, rx, ry = (x0 + x1) / 2, (y0 + y1) / 2, (x1 - x0) / 2 + 6, (y1 - y0) / 2 + 4
    a = arc(cx, cy, rx, ry, start, start + math.pi + 0.1)
    b = arc(cx, cy, rx, ry, start + math.pi, start + 2 * math.pi + 0.1)
    assert kinds(pg, [S(a, 0), S(b, 1)]) == [("circle", "Each session gets")]


# 10: margin bars
def test_bar_in_the_gutter_targets_text_not_numbers():
    words_line = []
    from remarkable_mcp.workflows.ink import Word

    y = 100.0
    words_line.append(Word("2", (40, y, 46, y + 14), 0, 0))
    for i, t in enumerate(["The", "paragraph", "text"]):
        x = 60 + i * 50
        words_line.append(Word(t, (x, y, x + 40, y + 14), 0, 0))
    words_line2 = [
        Word(w.text + "b", (w.rect[0], w.rect[1] + 18, w.rect[2], w.rect[3] + 18), 0, 1)
        for w in words_line[1:]
    ]
    pg = PageInk(
        1,
        0,
        446,
        595,
        strokes=[S(line(52, 52.5, 98, n=20) and [(52, 98 + t * 1.8) for t in range(20)])],
        words=words_line + words_line2,
    )
    [m] = analyze_page(pg)
    assert m.kind == "margin_bar" and "2" not in m.target_text.split()


def test_one_line_margin_bar(page):
    r, pg = page
    rects = words_of(r, "Ending the session")
    y0, y1 = rects[0][1] + 1, rects[0][3] - 1
    bar = [(rects[0][0] - 8 + 0.2 * math.sin(t), y0 + (y1 - y0) * t / 15) for t in range(16)]
    [(kind, _)] = kinds(pg, [S(bar)])
    assert kind == "margin_bar"


# 4: forms
@pytest.fixture(scope="module")
def form():
    return render_form(
        "F", [{"id": "c", "type": "choice", "label": "Pick", "options": ["Yes", "Later", "No"]}]
    )


def _answers(form, strokes):
    ink = load_document_ink_from_zip(_doc_zip(form.pdf, {0: [(s, 17, 446.0) for s in strokes]}))
    return {
        a.field["id"]: a
        for a in read_answers(form.manifest(), {p.pdf_page + 1: p for p in ink.pages})
    }


def _tick(rect, dx=0.0, dy=0.0, scale=1.0):
    x0, y0, x1, y1 = rect
    w, h = (x1 - x0) * scale, (y1 - y0) * scale
    x0, y0 = x0 + dx, y0 + dy
    a = [(x0 + 0.1 * w + 0.3 * w * t / 9, y0 + 0.5 * h + 0.35 * h * t / 9) for t in range(10)]
    return a + [
        (x0 + 0.4 * w + 0.7 * w * t / 14, y0 + 0.85 * h - 1.0 * h * t / 14) for t in range(15)
    ]


def test_tick_between_two_boxes_is_not_a_confident_answer(form):
    yes = next(a for a in form.areas if a.option == "Yes").rect
    later = next(a for a in form.areas if a.option == "Later").rect
    gap_mid = (yes[3] + later[1]) / 2
    tick = _tick(yes, dy=gap_mid - (yes[1] + yes[3]) / 2)
    ans = _answers(form, [tick])["c"]
    assert ans.status in ("ambiguous", "empty") and ans.status != "answered"


def test_tick_crossed_out_then_another_ticked(form):
    yes = next(a for a in form.areas if a.option == "Yes").rect
    no = next(a for a in form.areas if a.option == "No").rect
    x = [
        (yes[0] + (yes[2] - yes[0]) * t / 10, yes[1] + (yes[3] - yes[1]) * t / 10)
        for t in range(11)
    ]
    x2 = [
        (yes[2] - (yes[2] - yes[0]) * t / 10, yes[1] + (yes[3] - yes[1]) * t / 10)
        for t in range(11)
    ]
    ans = _answers(form, [_tick(yes), x, x2, _tick(no)])["c"]
    assert ans.value == "No" and ans.status == "answered"


def test_circling_the_option_label(form):
    later = next(a for a in form.areas if a.option == "Later")
    lr = later.label_rect
    cx, cy = (lr[0] + lr[2]) / 2, (lr[1] + lr[3]) / 2
    loop = arc(cx, cy, (lr[2] - lr[0]) / 2 + 6, (lr[3] - lr[1]) / 2 + 4, 0, 2 * math.pi * 1.05, 60)
    ans = _answers(form, [loop])["c"]
    assert ans.value == "Later" and ans.status == "answered"


# 1: inbox - underlining a request must not cancel it (generator from the
# adversarial review: cursive with ascender/descender spikes on a baseline)
def _cursive_word(x, base, w, rng, asc=True, desc=True):
    xh = rng.uniform(5, 7)
    n = int(w * 2.5)
    pts = []
    for i in range(n):
        t = i / (n - 1)
        y = base - xh / 2 - xh / 2 * math.sin(8 * math.pi * t)
        pts.append((x + w * t + 1.5 * math.cos(8 * math.pi * t), y))
    if asc:
        k = n // 3
        pts[k] = (pts[k][0], base - xh - rng.uniform(4, 6))
    if desc:
        k = 2 * n // 3
        pts[k] = (pts[k][0], base + rng.uniform(3, 5))
    return [(px + rng.uniform(-0.2, 0.2), py + rng.uniform(-0.2, 0.2)) for px, py in pts]


def _request(x, base, rng, words):
    out, cx = [], x
    for _ in range(words):
        w = rng.uniform(15, 40)
        out.append(_cursive_word(cx, base, w, rng, asc=rng.random() < 0.6, desc=rng.random() < 0.5))
        cx += w + rng.uniform(5, 9)
    return out


def _inbox_entries(strokes):
    pg = PageInk(
        page=1, pdf_page=0, width=446, height=595, strokes=[S(p, i) for i, p in enumerate(strokes)]
    )
    return [(len(e.strokes), e.cancelled) for e in segment_entries(pg)]


def _rule(k):
    from remarkable_mcp.workflows.inbox import LINE_PITCH, TEMPLATE_TOP

    return TEMPLATE_TOP + LINE_PITCH * (k + 1)


@pytest.mark.parametrize("seed", range(12))
def test_inbox_underline_is_not_a_cancel(seed):
    rng = random.Random(seed)
    req = _request(40, _rule(0) - 1, rng, rng.randint(3, 5))
    x1 = max(p[0] for s in req for p in s)
    y = _rule(0) - 1 + rng.uniform(-0.5, 1.5)  # underline touching the baseline
    under = line(38, x1 + 3, y, n=40, jitter=0.3, rng=rng)
    assert _inbox_entries(req + [under]) == [(len(req) + 1, False)] or all(
        not c for _, c in _inbox_entries(req + [under])
    )


@pytest.mark.parametrize("seed", range(12))
@pytest.mark.parametrize("words", [2, 4])
def test_inbox_strike_cancels(seed, words):
    rng = random.Random(100 + seed)
    req = _request(40, _rule(0) - 1, rng, words)
    x1 = max(p[0] for s in req for p in s)
    y = _rule(0) - 1 - rng.uniform(2, 4.5)
    strike = line(36, x1 + 4, y, n=40, jitter=0.3, rng=rng)
    entries = _inbox_entries(req + [strike])
    assert len(entries) == 1 and entries[0][1] is True


@pytest.mark.parametrize("seed", range(6))
def test_inbox_wavy_strike_cancels(seed):
    rng = random.Random(200 + seed)
    req = _request(40, _rule(0) - 1, rng, 3)
    x1 = max(p[0] for s in req for p in s)
    strike = line(36, x1 + 4, _rule(0) - 4, n=60, wave=1.0, period=8, rng=rng)
    assert _inbox_entries(req + [strike])[0][1] is True


# 11: sketch shapes
@pytest.mark.parametrize("aspect", [1.5, 2.0, 3.0])
def test_wide_ellipses_are_ellipses(aspect):
    ry = 30
    pts = arc(200, 200, ry * aspect, ry, 0, 2 * math.pi, 90)
    assert classify_outline(pts, []).kind == "ellipse"


@pytest.mark.parametrize("over", [0.05, 0.1])
def test_overshooting_ellipse_is_still_an_ellipse(over):
    pts = arc(200, 200, 60, 35, 0, 2 * math.pi * (1 + over), 100)
    assert classify_outline(pts, []).kind == "ellipse"


def test_rounded_rectangle_is_a_rectangle():
    pts = []
    x0, y0, x1, y1, rad = 50, 50, 250, 150, 14
    for cx, cy, a0 in (
        (x1 - rad, y0 + rad, -math.pi / 2),
        (x1 - rad, y1 - rad, 0),
        (x0 + rad, y1 - rad, math.pi / 2),
        (x0 + rad, y0 + rad, math.pi),
    ):
        pts += arc(cx, cy, rad, rad, a0, a0 + math.pi / 2, 8)
    pts.append(pts[0])
    assert classify_outline(pts, []).kind == "rect"


def test_closed_triangle_arrowhead_makes_a_directed_edge():
    from test_sketch import line_path, rect_path

    a = S(rect_path(50, 50, 150, 100), 0)
    b = S(rect_path(260, 50, 360, 100), 1)
    shaft = S(line_path((152, 75), (256, 75)), 2)
    head = S([(258, 75), (248, 70), (248, 80), (258, 75)], 3)
    [edge] = recognise([a, b, shaft, head]).edges
    assert edge.directed and edge.source == "n1" and edge.target == "n2"
