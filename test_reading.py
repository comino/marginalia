"""Tests for the reading queue: extraction, clip round trip, quotes with deep links."""

import asyncio
import builtins

import pytest

from remarkable_mcp.workflows import web
from test_workflows import (  # noqa: F401  (fixtures)
    FINELINER,
    HIGHLIGHTER,
    _fake_path,
    _hline,
    _json_of,
    _page_w,
    _phrase_rects,
    cloud,
)

PARA = (
    "Local-first software keeps the primary copy of your data on your own devices. "
    "Sync is an optimisation, not a dependency, and the network becomes optional. "
)
HTML = f"""<html><head><title>Local-first ideas | Example Blog</title></head><body>
<nav><a href="/">Home</a> <a href="/about">About</a></nav>
<article><h1>Local-first ideas</h1>
<p>{PARA * 2}</p>
<h2>Why it matters</h2>
<p>Users own their data and can work offline for days without noticing anything at all.
Collaboration still works because changes merge when devices reconnect later.</p>
<ul><li>Fast because reads are local</li><li>Private by default</li></ul>
<blockquote>The cloud should be a peer, not a master.</blockquote>
</article>
<footer>Copyright and newsletter signup noise</footer></body></html>"""


def test_fallback_extraction_keeps_structure(monkeypatch):
    real_import = builtins.__import__

    def no_trafilatura(name, *a, **k):
        if name == "trafilatura":
            raise ImportError
        return real_import(name, *a, **k)

    monkeypatch.setattr(builtins, "__import__", no_trafilatura)
    art = web.extract_article(HTML, "https://example.com/lf")
    assert art.extractor == "fallback"
    assert art.title == "Local-first ideas"
    assert "## Why it matters" in art.markdown
    assert "- Private by default" in art.markdown
    assert "> The cloud should be a peer" in art.markdown
    assert "Home" not in art.markdown and "newsletter" not in art.markdown


def test_trafilatura_extraction():
    pytest.importorskip("trafilatura")
    art = web.extract_article(HTML, "https://example.com/lf")
    assert art.extractor in ("trafilatura", "fallback")
    assert "primary copy of your data" in art.markdown
    assert "newsletter" not in art.markdown


def test_text_fragment_links():
    assert web.text_fragment_link("https://a.b/x#top", "short quote") == (
        "https://a.b/x#:~:text=short%20quote"
    )
    long = web.text_fragment_link(
        "https://a.b/x", "one two three four five six seven eight nine ten"
    )
    assert long.endswith("#:~:text=one%20two%20three%20four,seven%20eight%20nine%20ten")
    assert web.text_fragment_link(None, "x") is None


def test_clip_and_reading_notes(cloud):  # noqa: F811
    from remarkable_mcp.workflows import reading_tools as rt

    sent = _json_of(asyncio.run(rt.remarkable_clip(url="https://example.com/lf", html=HTML)))
    item = sent["item"]
    assert sent["title"] == "Local-first ideas"
    doc = next(d for d in cloud.docs.values() if d.VissibleName == "Local-first ideas")
    pdf = cloud.zips[doc.id]

    assert _json_of(asyncio.run(rt.remarkable_reading_list()))["items"][0]["status"] == "waiting"
    pno, rects = _phrase_rects(pdf, "Users own their data")
    y = (rects[0][1] + rects[0][3]) / 2
    cloud.annotate(
        doc.id, {pno: [(_hline(rects[0][0], rects[-1][2], y), HIGHLIGHTER, _page_w(pdf))]}
    )
    assert _json_of(asyncio.run(rt.remarkable_reading_list()))["items"][0]["status"] == "annotated"

    notes = _json_of(asyncio.run(rt.remarkable_reading_notes()))
    [entry] = notes["items"]
    [quote] = entry["quotes"]
    assert quote["kind"] == "highlight"
    assert quote["quote"] == "Users own their data"
    assert quote["link"] == "https://example.com/lf#:~:text=Users%20own%20their%20data"
    assert "> Users own their data" in entry["markdown"]

    again = _json_of(asyncio.run(rt.remarkable_reading_notes()))
    assert again["items"] == []
    assert _json_of(asyncio.run(rt.remarkable_reading_notes(item, only_new=False)))["items"][0][
        "quotes"
    ]


def test_clip_needs_input(cloud):  # noqa: F811
    from remarkable_mcp.workflows import reading_tools as rt

    assert _json_of(asyncio.run(rt.remarkable_clip()))["_error"]["type"] == "invalid_arguments"


def test_fragment_encodes_dashes_and_commas():
    link = web.text_fragment_link("https://a.b/x", "state-of-the-art, fast")
    assert link == "https://a.b/x#:~:text=state%2Dof%2Dthe%2Dart%2C%20fast"


def test_page_without_article_text_is_rejected(monkeypatch):
    real_import = builtins.__import__

    def no_trafilatura(name, *a, **k):
        if name == "trafilatura":
            raise ImportError
        return real_import(name, *a, **k)

    monkeypatch.setattr(builtins, "__import__", no_trafilatura)
    with pytest.raises(web.NoArticleText):
        web.extract_article("<html><body><div id=app></div><script>x()</script></body></html>")


def test_utf8_page_without_charset_header(monkeypatch):
    body = "<html><body><p>Grüße aus München</p></body></html>".encode()

    class Resp:
        headers = {"Content-Type": "text/html"}
        content = body
        encoding = None
        apparent_encoding = "utf-8"

        def raise_for_status(self):
            pass

        @property
        def text(self):
            return self.content.decode(self.encoding or "iso-8859-1")

    monkeypatch.setattr("requests.get", lambda *a, **k: Resp())
    assert "Grüße aus München" in web.fetch_html("https://example.com")


def test_reading_notes_mark_seen_false_keeps_marks_new(cloud):  # noqa: F811
    from remarkable_mcp.workflows import reading_tools as rt

    item = _json_of(asyncio.run(rt.remarkable_clip(url="https://example.com/lf", html=HTML)))[
        "item"
    ]
    doc = next(d for d in cloud.docs.values() if d.VissibleName == "Local-first ideas")
    pdf = cloud.zips[doc.id]
    pno, rects = _phrase_rects(pdf, "Users own their data")
    y = (rects[0][1] + rects[0][3]) / 2
    cloud.annotate(
        doc.id, {pno: [(_hline(rects[0][0], rects[-1][2], y), HIGHLIGHTER, _page_w(pdf))]}
    )
    peek = _json_of(asyncio.run(rt.remarkable_reading_notes(mark_seen=False)))
    assert peek["items"][0]["quotes"]
    again = _json_of(asyncio.run(rt.remarkable_reading_notes()))
    assert again["items"][0]["quotes"]  # still new: the peek did not consume them

    doc.parent = doc.Parent = "trash"
    err = _json_of(asyncio.run(rt.remarkable_reading_notes(item)))
    assert err["_error"]["type"] == "document_missing"
