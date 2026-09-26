"""Generated outputs must be valid for whatever consumes them.

- Mermaid: parsed with the real mermaid parser when available
  (MERMAID_NODE_MODULES=<dir containing node_modules/mermaid and jsdom>).
- HTML (wireframes): labels are escaped, markup is well formed.
- GitHub review payload: shape of the pull-request review API.
- Text fragments: reserved characters are percent-encoded.
"""

import json
import os
import shutil
import subprocess
from html.parser import HTMLParser
from pathlib import Path

import pytest

from remarkable_mcp.workflows.code_review import LineComment, comment_body, github_event
from remarkable_mcp.workflows.sketch import recognise, to_mermaid
from remarkable_mcp.workflows.web import text_fragment_link
from remarkable_mcp.workflows.wireframe import Element, to_html
from test_sketch import S, arrow_in_stroke, diamond_path, ellipse_path, rect_path

NASTY = [
    'say "hi"',
    "a | b",
    "[x]",
    "{y}",
    "(z)",
    "a -> b",
    "end",
    "#35;",
    "`code`",
    "<b>bold</b>",
    "line\nbreak",
    "ümlaut ß",
    "漢字",
    "--> arrow",
    ";semi",
    "%% comment",
    "click n1",
    "style",
    "",
    "   ",
    "a\\b",
    "100%",
]


def _mermaid_dir():
    cands = [os.environ.get("MERMAID_NODE_MODULES"), str(Path.home() / ".hermes" / "hermes-agent")]
    for c in cands:
        if (
            c
            and (Path(c) / "node_modules" / "mermaid").is_dir()
            and (Path(c) / "node_modules" / "jsdom").is_dir()
        ):
            return c
    return None


def _diagram():
    return recognise(
        [
            S(rect_path(50, 50, 150, 100)),
            S(ellipse_path(310, 75, 50, 25)),
            S(diamond_path(100, 260, 45, 35)),
            S(arrow_in_stroke((152, 75), (258, 75))),
        ]
    )


def _labelled_mermaids():
    out = []
    for label in NASTY:
        d = _diagram()
        for n in d.nodes:
            n.label = (
                n.label or type("L", (), {"text": None, "strokes": [], "rect": n.shape.rect})()
            )
            n.label.text = label
        for e in d.edges:
            e.label = type("L", (), {"text": label, "strokes": [], "rect": e.shape.rect})()
        out.append(to_mermaid(d))
    return out


@pytest.mark.skipif(not (shutil.which("node") and _mermaid_dir()), reason="needs node + mermaid")
def test_mermaid_output_parses_with_real_parser():
    diagrams = _labelled_mermaids()
    res = subprocess.run(
        ["node", str(Path(__file__).parent / "tests" / "mermaid_parse.mjs"), _mermaid_dir()],
        input=json.dumps(diagrams),
        capture_output=True,
        text=True,
        timeout=120,
    )
    results = json.loads(res.stdout.strip().splitlines()[-1])
    bad = [(NASTY[i], err) for i, err in enumerate(results) if err]
    assert not bad, bad


class _Strict(HTMLParser):
    VOID = {"meta", "input", "br", "img", "hr", "link"}

    def __init__(self):
        super().__init__()
        self.stack, self.scripts = [], 0

    def handle_starttag(self, tag, attrs):
        if tag == "script":
            self.scripts += 1
        if tag not in self.VOID:
            self.stack.append(tag)

    def handle_endtag(self, tag):
        assert self.stack and self.stack[-1] == tag, f"unbalanced </{tag}> in {self.stack}"
        self.stack.pop()


@pytest.mark.parametrize("label", NASTY + ['"><script>alert(1)</script>', "</div></body>"])
def test_wireframe_html_escapes_labels(label):
    elements = [
        Element("n1", "container", (0, 0, 300, 200), label=label),
        Element("n2", "button", (10, 10, 80, 30), label=label, parent="n1"),
        Element("n3", "input", (10, 40, 200, 60), label=label, parent="n1"),
        Element("t1", "heading", (10, 80, 200, 100), label=label),
        Element("i1", "image", (10, 110, 100, 190), label=label),
    ]
    page = to_html(elements, title=label)
    p = _Strict()
    p.feed(page)
    p.close()
    assert p.scripts == 0
    assert not p.stack, p.stack


def test_github_review_payload_shape():
    c = LineComment("src/a.py", 12, "RIGHT", "strikethrough", "x = 1", "m1", [], None)
    body = comment_body(c, "use a constant")
    payload = {
        "event": github_event("Request changes"),
        "body": "x",
        "comments": [{"path": c.path, "line": c.line, "side": c.side, "body": body}],
    }
    assert payload["event"] in {"APPROVE", "REQUEST_CHANGES", "COMMENT"}
    for com in payload["comments"]:
        assert set(com) == {"path", "line", "side", "body"}
        assert isinstance(com["line"], int) and com["line"] > 0
        assert com["side"] in {"LEFT", "RIGHT"} and com["body"].strip()
    json.dumps(payload)


@pytest.mark.parametrize("quote", ["a-b", "x, y", "50% off", "a&b=c", "#hash", "ümlaut"])
def test_text_fragments_encode_reserved_characters(quote):
    from urllib.parse import unquote

    frag = text_fragment_link("https://e.x/p", quote).split("#:~:text=", 1)[1]
    for ch in "-,&# ":
        assert ch not in frag, (ch, frag)  # reserved in text fragments / URLs
    assert unquote(frag) == quote
