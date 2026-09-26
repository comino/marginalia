"""Tests for LaTeX review via SyncTeX (compiles a tiny document; skipped without TeX)."""

import asyncio
import shutil
import subprocess
from pathlib import Path

import pytest

from remarkable_mcp.workflows.ink import load_document_ink_from_zip
from remarkable_mcp.workflows.latex_review import collect_tex_requests, synctex_edit
from test_workflows import (  # noqa: F401
    FINELINER,
    _doc_zip,
    _fake_path,
    _handwriting,
    _hline,
    _json_of,
    _phrase_rects,
    _strike,
    cloud,
)

pytestmark = pytest.mark.skipif(
    not (shutil.which("pdflatex") and shutil.which("synctex")), reason="needs TeX Live"
)

MAIN = r"""\documentclass[11pt]{article}
\usepackage[a4paper,margin=2.5cm]{geometry}
\begin{document}
\section{Introduction}
Multivariate forecasting models are evaluated on synthetic benchmarks
with controlled channel interactions.
\input{chapters/method}
\end{document}
"""
METHOD = r"""\section{Method}
We generate channels with known coupling strength.
The benchmark varies the lag structure and the noise level.
Every configuration is repeated with five random seeds.
"""


@pytest.fixture(scope="module")
def tex(tmp_path_factory):
    d = tmp_path_factory.mktemp("tex")
    (d / "chapters").mkdir()
    (d / "main.tex").write_text(MAIN)
    (d / "chapters" / "method.tex").write_text(METHOD)
    subprocess.run(
        ["pdflatex", "-synctex=1", "-interaction=nonstopmode", "main.tex"],
        cwd=d,
        capture_output=True,
        timeout=120,
        check=True,
    )
    return d


def test_synctex_edit_resolves_input_files(tex):
    pdf = tex / "main.pdf"
    pno, rects = _phrase_rects(pdf.read_bytes(), "coupling strength")
    x, y = (rects[0][0] + rects[0][2]) / 2, (rects[0][1] + rects[0][3]) / 2
    file, line = synctex_edit(pdf, pno + 1, x, y)
    assert Path(file).name == "method.tex" and line == 2


def test_marks_resolve_to_tex_lines(tex):
    pdf = tex / "main.pdf"
    data = pdf.read_bytes()
    pno, strike_rects = _phrase_rects(data, "Multivariate forecasting models")
    _, lag = _phrase_rects(data, "lag structure")
    note = _handwriting(520, lag[0][1], words=2, word_w=14, h=5)
    strokes = [(_strike(strike_rects), FINELINER, 595.0)] + [(p, FINELINER, 595.0) for p in note]
    ink = load_document_ink_from_zip(_doc_zip(data, {pno: strokes}))
    pages = {p.pdf_page + 1: p for p in ink.pages}
    reqs = {r.mark.kind: r for r in collect_tex_requests(pdf, pages)}
    strike = reqs["strikethrough"]
    assert Path(strike.file).name == "main.tex" and strike.line == 5
    assert "Multivariate forecasting models" in strike.source
    comment = reqs["note"]
    assert Path(comment.file).name == "method.tex" and comment.line == 3


def test_latex_tools_round_trip(tex, cloud, monkeypatch, tmp_path):  # noqa: F811
    from remarkable_mcp.workflows import latex_tools as t

    sent = _json_of(asyncio.run(t.remarkable_latex_review_send(str(tex / "main.pdf"), "Thesis")))
    doc = next(d for d in cloud.docs.values() if d.VissibleName == "Thesis")
    data = cloud.zips[doc.id]
    pno, rects = _phrase_rects(data, "five random seeds")
    cloud.annotate(doc.id, {pno: [(_strike(rects), FINELINER, 595.0)]})
    got = _json_of(asyncio.run(t.remarkable_latex_review_collect(sent["review"])))
    [req] = got["requests"]
    assert req["file"] == "chapters/method.tex" and req["line"] == 4
    assert req["intent"] == "delete" and req["target"] == "five random seeds."


def test_send_requires_synctex(tmp_path, cloud):  # noqa: F811
    from remarkable_mcp.workflows import latex_tools as t

    pdf = tmp_path / "x.pdf"
    pdf.write_bytes(b"%PDF-1.4\n")
    assert (
        _json_of(asyncio.run(t.remarkable_latex_review_send(str(pdf))))["_error"]["type"]
        == "no_synctex"
    )
