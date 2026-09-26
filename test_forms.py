"""Tests for paper forms: rendering geometry and reading pen answers."""

import asyncio
import json

import pymupdf
import pytest

from remarkable_mcp.workflows.forms import FormSpecError, read_answers, render_form, stray_strokes
from remarkable_mcp.workflows.ink import load_document_ink_from_zip
from test_workflows import (  # noqa: F401  (fixtures)
    FINELINER,
    FakeCloud,
    _doc_zip,
    _ellipse,
    _fake_path,
    _handwriting,
    _json_of,
    cloud,
)

FIELDS = [
    {"type": "info", "label": "Context line."},
    {"id": "publish", "type": "choice", "label": "Publish?", "options": ["Yes", "Later", "No"]},
    {"id": "where", "type": "multi", "label": "Where?", "options": ["LinkedIn", "HN", "Mail"]},
    {"id": "ready", "type": "scale", "label": "Ready?", "min": 1, "max": 5},
    {"id": "cover", "type": "checkbox", "label": "Make a cover image"},
    {"id": "notes", "type": "text", "label": "Notes", "lines": 2},
]


def _area(render, field, option=None):
    return next(a for a in render.areas if a.field_id == field and a.option == option)


def _tick(rect):
    x0, y0, x1, y1 = rect
    w, h = x1 - x0, y1 - y0
    pts = [(x0 + 0.1 * w + 0.3 * w * t / 9, y0 + 0.5 * h + 0.35 * h * t / 9) for t in range(10)]
    pts += [(x0 + 0.4 * w + 0.7 * w * t / 14, y0 + 0.85 * h - 1.0 * h * t / 14) for t in range(15)]
    return pts


def _cross(rect):
    x0, y0, x1, y1 = rect
    a = [(x0 + (x1 - x0) * t / 12, y0 + (y1 - y0) * t / 12) for t in range(13)]
    b = [(x1 - (x1 - x0) * t / 12, y0 + (y1 - y0) * t / 12) for t in range(13)]
    return a, b


def _fill(rect, passes=14):
    x0, y0, x1, y1 = rect
    pts = []
    for p in range(passes):
        y = y0 + (y1 - y0) * p / (passes - 1)
        pts += [(x0, y), (x1, y)] if p % 2 == 0 else [(x1, y), (x0, y)]
    return pts


def _read(render, strokes):
    zip_bytes = _doc_zip(render.pdf, {0: [(pts, FINELINER, 446.0) for pts in strokes]})
    ink = load_document_ink_from_zip(zip_bytes)
    pages = {p.pdf_page + 1: p for p in ink.pages}
    return {a.field["id"]: a for a in read_answers(render.manifest(), pages)}, pages


@pytest.fixture
def form():
    return render_form("Decision", FIELDS, intro="Tick or circle.")


def test_blank_form_reads_empty(form):
    answers, _ = _read(form, [])
    assert answers["publish"].value is None and answers["publish"].status == "empty"
    assert answers["where"].value == []
    assert answers["cover"].value is False
    assert answers["notes"].status == "empty"


def test_tick_cross_and_circle(form):
    later = _area(form, "publish", "Later").rect
    hn_a, hn_b = _cross(_area(form, "where", "HN").rect)
    four = _area(form, "ready", "4").rect
    circle = _ellipse(four[0] - 3, four[1] - 3, four[2] + 3, four[3] + 3, loops=1.05)
    cover = _area(form, "cover").rect
    answers, _ = _read(form, [_tick(later), hn_a, hn_b, circle, _tick(cover)])
    assert answers["publish"].value == "Later"
    assert answers["publish"].status == "answered"
    assert answers["where"].value == ["HN"]
    assert answers["ready"].value == 4
    assert answers["cover"].value is True


def test_filled_box_cancels_a_tick(form):
    yes = _area(form, "publish", "Yes").rect
    no = _area(form, "publish", "No").rect
    answers, _ = _read(form, [_fill(yes), _tick(no)])
    assert answers["publish"].value == "No"
    assert answers["publish"].detail["cancelled"] == ["Yes"]


def test_two_ticks_in_single_choice_is_ambiguous(form):
    yes = _area(form, "publish", "Yes").rect
    no = _area(form, "publish", "No").rect
    answers, _ = _read(form, [_tick(yes), _tick(no)])
    assert answers["publish"].status == "ambiguous"
    assert set(answers["publish"].detail["candidates"]) == {"Yes", "No"}


def test_write_in_and_remarks(form):
    notes = _area(form, "notes").rect
    writing = _handwriting(notes[0] + 10, notes[1] + 8, words=3)
    remark = _handwriting(300, 70, words=1)
    answers, pages = _read(form, writing + remark)
    assert answers["notes"].status == "needs_transcription"
    assert len(answers["notes"].strokes) == 3
    extra = stray_strokes(form.manifest(), pages)
    assert sum(len(v) for v in extra.values()) == 1


def test_spec_validation():
    with pytest.raises(FormSpecError):
        render_form("x", [])
    with pytest.raises(FormSpecError):
        render_form("x", [{"type": "choice", "label": "a", "options": ["only"]}])
    with pytest.raises(FormSpecError):
        render_form("x", [{"id": "a", "label": "a"}, {"id": "a", "label": "b"}])
    with pytest.raises(FormSpecError):
        render_form("x", [{"type": "info", "label": "no questions"}])


def test_long_forms_paginate():
    fields = [{"type": "choice", "label": f"Q{i}", "options": ["a", "b", "c"]} for i in range(12)]
    r = render_form("Long", fields)
    assert r.page_count >= 2
    assert {a.page for a in r.areas} == set(range(1, r.page_count + 1))


def test_ask_round_trip(cloud):  # noqa: F811
    from remarkable_mcp.workflows import form_tools

    sent = _json_of(asyncio.run(form_tools.remarkable_ask("Publish on Tuesday?")))
    form_id = sent["form"]
    first = _json_of(asyncio.run(form_tools.remarkable_form_read(form_id)))
    assert first["answered"] is False

    record = form_tools._forms().get(form_id)
    doc_id = record["doc_id"]
    yes = next(a for a in record["manifest"]["areas"] if a["option"] == "Yes")
    cloud.annotate(doc_id, {0: [(_tick(tuple(yes["rect"])), FINELINER, 446.0)]})

    listed = _json_of(asyncio.run(form_tools.remarkable_form_list()))
    assert listed["forms"][0]["status"] == "annotated"
    got = _json_of(asyncio.run(form_tools.remarkable_form_read(form_id)))
    assert got["answered"] is True
    assert got["values"]["answer"] == "Yes"
    assert got["values"]["comment"] is None
    assert (
        _json_of(asyncio.run(form_tools.remarkable_form_list()))["forms"][0]["status"] == "waiting"
    )


def test_form_send_rejects_bad_spec(cloud):  # noqa: F811
    from remarkable_mcp.workflows import form_tools

    err = json.loads(
        asyncio.run(form_tools.remarkable_form_send("Bad", [{"type": "nope", "label": "x"}]))
    )
    assert err["_error"]["type"] == "invalid_form"


def test_circle_is_credited_to_one_option_only(form):
    later = _area(form, "publish", "Later").rect
    loop = _ellipse(later[0] - 4, later[1] - 12, later[2] + 60, later[3] + 12, loops=1.05)
    three = _area(form, "ready", "3").rect
    wide = _ellipse(three[0] - 22, three[1] - 4, three[2] + 22, three[3] + 4, loops=1.05)
    answers, pages = _read(form, [loop, wide])
    assert answers["publish"].value == "Later" and answers["publish"].status == "answered"
    assert answers["ready"].value == 3 and answers["ready"].status == "answered"
    assert stray_strokes(form.manifest(), pages) == {}


def test_long_labels_stay_with_their_boxes():
    long = "A deliberately long option label that wraps onto a second line in the form " * 2
    fields = [
        {"type": "choice", "label": f"Q{i}", "options": [long, "short", long]} for i in range(6)
    ]
    r = render_form("Wrapping", fields)
    with pymupdf.open(stream=r.pdf, filetype="pdf") as doc:
        for a in r.areas:
            words = [
                w
                for w in doc[a.page - 1].get_text("words")
                if abs(w[1] - a.rect[1]) < 6 and w[0] > a.rect[2]
            ]
            assert words, f"box on page {a.page} has no label beside it"


def test_form_with_only_checkboxes_is_answered_once_touched(cloud):  # noqa: F811
    from remarkable_mcp.workflows import form_tools

    sent = _json_of(
        asyncio.run(
            form_tools.remarkable_form_send(
                "Checks",
                [
                    {"id": "a", "type": "checkbox", "label": "A"},
                    {"id": "b", "type": "checkbox", "label": "B"},
                ],
            )
        )
    )
    record = form_tools._forms().get(sent["form"])
    assert _json_of(asyncio.run(form_tools.remarkable_form_read(sent["form"])))["answered"] is False
    box_a = next(a for a in record["manifest"]["areas"] if a["field"] == "a")
    cloud.annotate(record["doc_id"], {0: [(_tick(tuple(box_a["rect"])), FINELINER, 446.0)]})
    got = _json_of(asyncio.run(form_tools.remarkable_form_read(sent["form"])))
    assert got["answered"] is True
    assert got["values"] == {"a": True, "b": False}
