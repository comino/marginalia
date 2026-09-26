"""Fetch a web article and reduce it to clean Markdown for tablet reading.

Uses trafilatura (the ``web`` extra) when installed - it handles boilerplate,
comments and navigation far better than any heuristic. Without it, a
BeautifulSoup fallback keeps headings, paragraphs, lists, quotes and code from
the page's main content element.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Optional
from urllib.parse import quote

from remarkable_mcp.workflows.safety import fetch_public

_UA = "Mozilla/5.0 (X11; Linux aarch64) remarkable-mcp reading-queue"


@dataclass
class Article:
    title: str
    markdown: str
    url: Optional[str] = None
    author: Optional[str] = None
    date: Optional[str] = None
    site: Optional[str] = None
    extractor: str = "fallback"


MAX_PAGE_BYTES = 10_000_000


def fetch_html(url: str, timeout: float = 20.0) -> str:
    """Fetch a public web page (no local/private addresses, capped size)."""
    resp = fetch_public(url, {"User-Agent": _UA}, timeout, MAX_PAGE_BYTES)
    # requests assumes ISO-8859-1 for text/* without a charset; most pages are UTF-8.
    if "charset" not in resp.headers.get("Content-Type", "").lower():
        resp.encoding = resp.apparent_encoding or "utf-8"
    return resp.text


class NoArticleText(ValueError):
    """The page has no extractable article text (e.g. rendered by JavaScript)."""


def extract_article(html: str, url: Optional[str] = None) -> Article:
    try:
        return _with_trafilatura(html, url)
    except ImportError:
        pass
    except Exception:  # found nothing or choked on the page; try the heuristic
        pass
    art = _with_soup(html, url)
    if len(art.markdown.split()) < 30:
        raise NoArticleText(
            "No readable article text on this page (it may be rendered by JavaScript). "
            "Pass the text as markdown= instead."
        )
    return art


def _with_trafilatura(html: str, url: Optional[str]) -> Article:
    import trafilatura

    md = trafilatura.extract(
        html,
        url=url,
        output_format="markdown",
        include_comments=False,
        include_tables=True,
        include_formatting=True,
        include_links=False,
        favor_precision=True,
    )
    if not md or len(md.split()) < 30:
        raise ValueError("no article text")
    meta = trafilatura.extract_metadata(html, default_url=url)
    title = (meta.title if meta and meta.title else None) or _title_from_html(html) or "Article"
    return Article(
        title=title.strip(),
        markdown=md.strip(),
        url=url,
        author=getattr(meta, "author", None) if meta else None,
        date=getattr(meta, "date", None) if meta else None,
        site=getattr(meta, "sitename", None) if meta else None,
        extractor="trafilatura",
    )


def _title_from_html(html: str) -> Optional[str]:
    m = re.search(r"<title[^>]*>(.*?)</title>", html, re.IGNORECASE | re.DOTALL)
    return " ".join(m.group(1).split()) if m else None


def _with_soup(html: str, url: Optional[str]) -> Article:
    from bs4 import BeautifulSoup

    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(["script", "style", "nav", "header", "footer", "aside", "form", "noscript"]):
        tag.decompose()
    root = soup.find("article") or soup.find("main")
    if root is None:
        candidates = soup.find_all(["div", "section"])
        root = max(
            candidates, key=lambda el: len(el.find_all("p", recursive=False)), default=soup.body
        )
    title = None
    h1 = soup.find("h1")
    if h1:
        title = h1.get_text(" ", strip=True)
    title = title or _title_from_html(html) or "Article"

    lines = []
    for el in (root or soup).find_all(["h1", "h2", "h3", "h4", "p", "li", "blockquote", "pre"]):
        if el.find_parent(["blockquote", "pre", "li"]) and el.name != "li":
            continue
        text = el.get_text(" ", strip=True)
        if not text or (el.name == "h1" and text == title):
            continue
        if el.name in ("h1", "h2", "h3", "h4"):
            lines.append("#" * max(2, int(el.name[1])) + " " + text)
        elif el.name == "li":
            lines.append("- " + text)
            continue
        elif el.name == "blockquote":
            lines.append("> " + text)
        elif el.name == "pre":
            lines.append("```\n" + el.get_text() + "\n```")
        else:
            lines.append(text)
        lines.append("")
    md = "\n".join(lines).strip()
    return Article(title=title, markdown=md, url=url)


def text_fragment_link(url: Optional[str], quote_text: str, max_words: int = 8) -> Optional[str]:
    """A link that scrolls to and highlights ``quote_text`` (URL text fragment)."""
    if not url or not quote_text:
        return None
    words = quote_text.split()
    if not words:
        return None
    base = url.split("#", 1)[0]

    def enc(text: str) -> str:
        # "-" and "," are syntax in text fragments and must be percent-encoded.
        return quote(text, safe="").replace("-", "%2D")

    if len(words) <= max_words:
        frag = enc(" ".join(words))
    else:
        frag = f"{enc(' '.join(words[:4]))},{enc(' '.join(words[-4:]))}"
    return f"{base}#:~:text={frag}"
