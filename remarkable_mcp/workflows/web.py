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


def fetch_html(url: str, timeout: float = 20.0) -> str:
    import requests

    resp = requests.get(url, headers={"User-Agent": _UA}, timeout=timeout)
    resp.raise_for_status()
    resp.encoding = resp.encoding or resp.apparent_encoding
    return resp.text


def extract_article(html: str, url: Optional[str] = None) -> Article:
    try:
        return _with_trafilatura(html, url)
    except ImportError:
        pass
    except ValueError:
        pass  # trafilatura found nothing; try the heuristic
    return _with_soup(html, url)


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
    return Article(title=title, markdown=md or "(no readable text found)", url=url)


def text_fragment_link(url: Optional[str], quote_text: str, max_words: int = 8) -> Optional[str]:
    """A link that scrolls to and highlights ``quote_text`` (URL text fragment)."""
    if not url or not quote_text:
        return None
    words = quote_text.split()
    if not words:
        return None
    base = url.split("#", 1)[0]
    if len(words) <= max_words:
        frag = quote(" ".join(words), safe="")
    else:
        start = quote(" ".join(words[:4]), safe="")
        end = quote(" ".join(words[-4:]), safe="")
        frag = f"{start},{end}"
    return f"{base}#:~:text={frag}"
