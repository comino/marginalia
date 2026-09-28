"""Regenerate the README gallery from the real engines.

Every image and output here is produced by Marginalia itself: synthetic pen
ink (Hershey single-stroke fonts for handwriting, hand-wobbled lines and
loops for marks) is written into real v6 ``.rm`` files on the PDFs the tools
render, then read back through the same code the MCP tools use. No real
user ink is involved.

    uv run --with Hershey-Fonts python docs/assets/make_gallery.py

Handwriting transcription needs a backend (MyScript, Google Vision, Claude).
The gallery fills in the text the synthetic handwriting spells, which is what
a configured backend returns; everything else is the engines' own output.
"""

from __future__ import annotations

import io
import json
import math
import random
import sys
from pathlib import Path

import pymupdf
from HersheyFonts import HersheyFonts
from PIL import Image

ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT), str(ROOT / "tests")]

from remarkable_mcp.workflows.forms.forms import read_answers, render_form  # noqa: E402
from remarkable_mcp.workflows.inbox.inbox import (  # noqa: E402
    LINE_PITCH,
    TEMPLATE_TOP,
    render_inbox_template,
    segment_entries,
)
from remarkable_mcp.workflows.ink.page import load_document_ink_from_zip  # noqa: E402
from remarkable_mcp.workflows.review.markdown import collect_requests  # noqa: E402
from remarkable_mcp.workflows.review.render import render_review_pdf  # noqa: E402
from remarkable_mcp.workflows.structure.sketch import (  # noqa: E402
    recognise,
    summary,
    to_mermaid,
    to_svg,
)
from test_workflows import FINELINER, _doc_zip, _phrase_rects  # noqa: E402

OUT = Path(__file__).resolve().parent / "gallery"
INK = (0.10, 0.20, 0.55)  # blue-black fineliner
RNG = random.Random(7)

# --------------------------------------------------------------------------- pen


def wobble(points, amount=0.35, seed=None):
    """A hand does not draw straight: add a smooth, low-frequency wobble."""
    rng = random.Random(seed if seed is not None else RNG.random())
    phase, freq = rng.uniform(0, 6.3), rng.uniform(0.08, 0.16)
    return [
        (x + amount * math.sin(phase + i * freq), y + amount * math.cos(1.7 * phase + i * freq))
        for i, (x, y) in enumerate(points)
    ]


def densify(points, step=1.5):
    out = [points[0]]
    for a, b in zip(points, points[1:]):
        n = max(1, int(math.dist(a, b) / step))
        out += [
            (a[0] + (b[0] - a[0]) * t / n, a[1] + (b[1] - a[1]) * t / n) for t in range(1, n + 1)
        ]
    return out


_FONT = HersheyFonts()
_FONT.load_default_font("scripts")


def pen_text(text, x, baseline, size=11.0, slant=0.18):
    """Handwriting: Hershey single-stroke script, slanted and wobbled."""
    _FONT.normalize_rendering(size)
    strokes = []
    for seg in _FONT.strokes_for_text(text):
        pts = [(x + px + slant * py, baseline - py) for px, py in seg]
        if len(pts) >= 2:
            strokes.append(wobble(densify(pts, 0.8), 0.12))
    return strokes


def hline(x0, x1, y, overshoot=3.0, tilt=0.6):
    pts = densify([(x0 - overshoot, y + tilt / 2), (x1 + overshoot, y - tilt / 2)])
    return wobble(pts, 0.45)


def loop(x0, y0, x1, y1, pad=(7, 5), turns=1.08):
    cx, cy = (x0 + x1) / 2, (y0 + y1) / 2
    rx, ry = (x1 - x0) / 2 + pad[0], (y1 - y0) / 2 + pad[1]
    n = 90
    a0 = RNG.uniform(2.6, 3.4)
    return wobble(
        [
            (
                cx + rx * math.cos(a0 + 2 * math.pi * turns * i / n),
                cy + ry * math.sin(a0 + 2 * math.pi * turns * i / n),
            )
            for i in range(n + 1)
        ],
        0.9,
    )


def box(x0, y0, x1, y1):
    corners = [(x0 + 1, y0), (x1, y0 + 1), (x1 - 1, y1), (x0, y1 - 1), (x0 + 2, y0 - 1)]
    return wobble(densify(corners, 2.0), 0.7)


def diamond(cx, cy, rx, ry):
    pts = [(cx, cy - ry), (cx + rx, cy), (cx, cy + ry), (cx - rx, cy), (cx + 1, cy - ry - 1)]
    return wobble(densify(pts, 2.0), 0.6)


def arrow(a, b, head=8.0):
    shaft = wobble(densify([a, b], 2.0), 0.5)
    ang = math.atan2(b[1] - a[1], b[0] - a[0])
    left = (b[0] - head * math.cos(ang - 0.45), b[1] - head * math.sin(ang - 0.45))
    right = (b[0] - head * math.cos(ang + 0.45), b[1] - head * math.sin(ang + 0.45))
    return [shaft, wobble(densify([left, b, right], 1.0), 0.2)]


def tick(rect):
    x0, y0, x1, y1 = rect
    w, h = x1 - x0, y1 - y0
    return wobble(
        densify(
            [
                (x0 + 0.05 * w, y0 + 0.5 * h),
                (x0 + 0.4 * w, y0 + 0.95 * h),
                (x1 + 0.4 * w, y0 - 0.5 * h),
            ],
            1.0,
        ),
        0.25,
    )


# --------------------------------------------------------------------------- render


def read_ink(pdf, strokes_by_page):
    """Write the strokes into a real document zip and read them back."""
    with pymupdf.open(stream=pdf, filetype="pdf") as doc:
        width = doc[0].rect.width
    zipped = _doc_zip(
        pdf, {p: [(s, FINELINER, width) for s in ss] for p, ss in strokes_by_page.items()}
    )
    return load_document_ink_from_zip(zipped)


def render(pdf, page, strokes, clip, name, dpi=170, width=1.25):
    """The page as the tablet shows it: PDF plus ink, cropped to ``clip``."""
    doc = pymupdf.open(stream=pdf, filetype="pdf")
    pg = doc[page]
    shape = pg.new_shape()
    for s in strokes:
        shape.draw_polyline(s)
    shape.finish(color=INK, width=width, closePath=False, lineCap=1, lineJoin=1)
    shape.commit()
    pix = pg.get_pixmap(dpi=dpi, clip=pymupdf.Rect(*clip))
    img = Image.open(io.BytesIO(pix.tobytes("png"))).convert("RGB")
    img = img.quantize(colors=48, method=Image.Quantize.MEDIANCUT)
    OUT.mkdir(parents=True, exist_ok=True)
    img.save(OUT / name, optimize=True)
    return OUT / name


def dump(name, data):
    (OUT / name).write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n")


# --------------------------------------------------------------------------- panels

DRAFT = """# A Disposable DuckDB Workspace

Agents are really very good at SQL once the data sits in a single file they
can open, query and throw away.

DuckDB gives them exactly that: one process, no server, and it reads Parquet
and CSV straight from disk without an import step.

The trick is to keep the workspace disposable. Every session starts from a
fresh copy, so a bad query can never damage the source data.
"""


def review_panel():
    r = render_review_pdf(DRAFT, legend=False)
    pdf = r.pdf
    _, strike = _phrase_rects(pdf, "really very")
    _, circ = _phrase_rects(pdf, "no server")
    _, under = _phrase_rects(pdf, "a fresh copy")
    with pymupdf.open(stream=pdf, filetype="pdf") as doc:
        page_w = doc[0].rect.width
    y_strike = (strike[0][1] + strike[0][3]) / 2 + 0.6
    y_under = under[0][3] - 0.6
    note_x = page_w - 118
    strokes = [
        hline(strike[0][0], strike[-1][2], y_strike),
        loop(circ[0][0], circ[0][1], circ[-1][2], circ[0][3]),
        *pen_text("say: zero setup", note_x, circ[0][3] + 1, 10.5),
        hline(under[0][0], under[-1][2], y_under, overshoot=1.5, tilt=0.3),
        *pen_text("love it!", note_x, y_under + 3, 10.5),
    ]
    ink = read_ink(pdf, {0: strokes})
    notes = {"circle": "say: zero setup", "underline": "love it!"}
    out = []
    for req in collect_requests(ink, r.manifest_blocks(), DRAFT):
        d = req.to_dict(
            notes.get(req.mark.kind), "transcribed" if req.mark.kind in notes else "none"
        )
        d.pop("id", None)
        d.pop("context", None)
        out.append(d)
    ys = [p[1] for s in strokes for p in s]
    clip = (30, min(ys) - 48, page_w - 8, max(ys) + 22)
    render(pdf, 0, strokes, clip, "review.png")
    dump("review.json", out)
    hero(pdf, strokes, clip, out)
    return out


# --------------------------------------------------------------------------- hero

CYCLE = 11.0  # seconds per loop


def _path(points):
    return "M" + " L".join(f"{x:.1f} {y:.1f}" for x, y in points)


def _length(points):
    return sum(math.dist(a, b) for a, b in zip(points, points[1:]))


def _json_lines(requests):
    """The collected requests as short, syntax-coloured lines."""
    key, text, num, punct = "#79c0ff", "#a5d6ff", "#ffa657", "#8b949e"
    lines = [[("[", punct)]]
    for i, r in enumerate(requests):
        lines.append(
            [
                ("  { ", punct),
                ('"intent"', key),
                (": ", punct),
                (f'"{r["intent"]}"', text),
                (", ", punct),
                ('"src_line"', key),
                (": ", punct),
                (str(r["src_line"]), num),
                (",", punct),
            ]
        )
        lines.append(
            [
                ("    ", punct),
                ('"target"', key),
                (": ", punct),
                (f'"{r["target"]}"', text),
                ("," if "note" in r else "", punct),
            ]
        )
        if "note" in r:
            lines.append(
                [("    ", punct), ('"note"', key), (": ", punct), (f'"{r["note"]}"', text)]
            )
        lines.append([("  }" + ("," if i < len(requests) - 1 else ""), punct)])
    lines.append([("]", punct)])
    return lines


def hero(pdf, strokes, clip, requests):
    """docs/assets/hero.svg: the review page, the pen marks drawing themselves,
    then the requests remarkable_review_collect returns for them."""
    with pymupdf.open(stream=pdf, filetype="pdf") as doc:
        page_svg = doc[0].get_svg_image(text_as_path=True)
    inner = page_svg[page_svg.index(">", page_svg.index("<svg")) + 1 : page_svg.rindex("</svg>")]
    x0, y0, x1, y1 = clip
    css, ink = [], []

    # Pen strokes in drawing order, each gets its slice of the 3%..37% window.
    total = sum(_length(s) for s in strokes)
    t = 3.0
    for i, st in enumerate(strokes):
        length = _length(st)
        dur = max(0.4, 31.0 * length / total)
        a, b = t, min(t + dur, 38.0)
        t = b + 0.3
        css.append(
            f"@keyframes s{i}{{0%,{a:.1f}%{{stroke-dashoffset:{length:.0f}}}"
            f"{b:.1f}%,100%{{stroke-dashoffset:0}}}}"
            f".s{i}{{stroke-dasharray:{length:.0f};stroke-dashoffset:{length:.0f};"
            f"animation:s{i} {CYCLE}s linear infinite}}"
        )
        ink.append(f'<path class="s{i}" d="{_path(st)}"/>')

    lines = _json_lines(requests)
    card = []
    for i, parts in enumerate(lines):
        a = 45.0 + i * 1.2
        css.append(
            f"@keyframes j{i}{{0%,{a:.1f}%{{opacity:0;transform:translateY(6px)}}"
            f"{a + 1.5:.1f}%,100%{{opacity:1;transform:none}}}}"
            f".j{i}{{opacity:0;animation:j{i} {CYCLE}s ease-out infinite}}"
        )
        spans = "".join(
            f'<tspan fill="{color}">{txt.replace("&", "&amp;").replace("<", "&lt;")}</tspan>'
            for txt, color in parts
        )
        card.append(
            f'<text class="j{i}" x="656" y="{106 + 19 * i}" xml:space="preserve">{spans}</text>'
        )

    svg = f"""<svg xmlns="http://www.w3.org/2000/svg" xmlns:xlink="http://www.w3.org/1999/xlink" viewBox="0 0 1000 400" width="1000" height="400" role="img" aria-labelledby="t d">
<title id="t">Marginalia: pen marks in, change requests out</title>
<desc id="d">A draft on the reMarkable gets a strike, a circle with a margin note and an underline with a note; Marginalia returns them as change requests with source lines.</desc>
<style>
.ink{{fill:none;stroke:#1a3390;stroke-width:1.35;stroke-linecap:round;stroke-linejoin:round}}
.code{{font:13px ui-monospace,SFMono-Regular,Menlo,Consolas,monospace}}
.call{{font:12px ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;fill:#8b949e}}
@keyframes fade{{0%,92%{{opacity:1}}97%,100%{{opacity:0}}}}
.cycle{{animation:fade {CYCLE}s linear infinite}}
@keyframes arrow{{0%,39%{{stroke-dashoffset:60;opacity:0}}40%{{opacity:1}}44%,100%{{stroke-dashoffset:0;opacity:1}}}}
.arrow{{stroke-dasharray:60;animation:arrow {CYCLE}s ease-out infinite}}
{chr(10).join(css)}
@media (prefers-reduced-motion: reduce){{*{{animation:none!important;stroke-dashoffset:0!important;opacity:1!important;transform:none!important}}}}
</style>
<rect x="16" y="16" width="568" height="368" rx="16" fill="#fbfaf6" stroke="#d6d1c4"/>
<svg x="26" y="26" width="548" height="348" viewBox="{x0:.1f} {y0:.1f} {x1 - x0:.1f} {y1 - y0:.1f}" preserveAspectRatio="xMidYMid meet">
{inner}
<g class="ink cycle">{"".join(ink)}</g>
</svg>
<g class="cycle">
<path class="arrow" d="M592 200 C 606 196, 614 196, 626 200" fill="none" stroke="#8b949e" stroke-width="2" stroke-linecap="round"/>
<path class="arrow" d="M619 193 L627 200 L619 207" fill="none" stroke="#8b949e" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/>
</g>
<rect x="636" y="36" width="352" height="328" rx="14" fill="#0d1117" stroke="#30363d"/>
<circle cx="658" cy="58" r="5" fill="#ff5f57"/><circle cx="676" cy="58" r="5" fill="#febc2e"/><circle cx="694" cy="58" r="5" fill="#28c840"/>
<text class="call" x="712" y="62">remarkable_review_collect("duckdb")</text>
<g class="code cycle">{"".join(card)}</g>
</svg>
"""
    (OUT.parent / "hero.svg").write_text(svg)


FORM_FIELDS = [
    {
        "id": "publish",
        "type": "choice",
        "label": "Publish the DuckDB post?",
        "options": ["Yes", "Later", "No"],
    },
    {"id": "where", "type": "multi", "label": "Where?", "options": ["Blog", "LinkedIn", "HN"]},
    {"id": "notes", "type": "text", "label": "Anything else?", "lines": 2},
]


def form_panel():
    f = render_form("Quick question", FORM_FIELDS)
    area = {(a.field_id, a.option): a.rect for a in f.areas}
    notes = area[("notes", None)]
    strokes = [
        tick(area[("publish", "Yes")]),
        tick(area[("where", "Blog")]),
        tick(area[("where", "HN")]),
        *pen_text("after the typo fix", notes[0] + 8, notes[1] + 18, 11),
    ]
    ink = read_ink(f.pdf, {0: strokes})
    answers = read_answers(f.manifest(), {p.pdf_page + 1: p for p in ink.pages})
    values = {}
    for a in answers:
        values[a.field["id"]] = (
            "after the typo fix" if a.status == "needs_transcription" else a.value
        )
    out = {"answered": all(a.status != "empty" for a in answers), "values": values}
    rects = [a.rect for a in f.areas]
    clip = (24, min(r[1] for r in rects) - 34, 446 - 24, max(r[3] for r in rects) + 12)
    render(f.pdf, 0, strokes, clip, "form.png")
    dump("form.json", out)
    return out


INBOX = [
    ("Summarise the DuckDB post for LinkedIn #blog", False),
    ("Book a train to Munich for Friday #travel", True),
    ("Find three papers on multivariate forecasting #thesis", False),
]


def inbox_panel():
    pdf = render_inbox_template(pages=1)
    strokes, k = [], 0
    for text, cancelled in INBOX:
        base = TEMPLATE_TOP + LINE_PITCH * (k + 1) - 2
        line = pen_text(text, 44, base, 11.5)
        strokes += line
        if cancelled:  # struck through the middle of the letters, not under them
            xs = sorted(p[0] for s in line for p in s)
            ys = sorted(p[1] for s in line for p in s)
            strokes.append(hline(xs[0], xs[-1], ys[len(ys) // 2], overshoot=4))
        k += 2
    ink = read_ink(pdf, {0: strokes})
    entries = segment_entries(ink.pages[0])
    out = []
    for e, (text, _) in zip(entries, INBOX):
        out.append(
            {
                "id": e.id,
                "status": "cancelled" if e.cancelled else "pending",
                "text": text.split(" #")[0],
                "tags": [w[1:] for w in text.split() if w.startswith("#")],
            }
        )
    ys = [p[1] for s in strokes for p in s]
    render(pdf, 0, strokes, (24, TEMPLATE_TOP - 70, 446 - 24, max(ys) + 26), "inbox.png")
    dump("inbox.json", out)
    return out


def sketch_panel():
    """A whiteboard sketch on a blank page: the Marginalia loop."""
    doc = pymupdf.open()
    doc.new_page(width=446, height=260)
    pdf = doc.tobytes()
    labels = {}  # label text -> centre of where it is written
    strokes = []

    def label(text, cx, cy, size=12.5):
        _FONT.normalize_rendering(size)
        w = max((px for seg in _FONT.strokes_for_text(text) for px, _ in seg), default=0)
        labels[text] = (cx, cy)
        return pen_text(text, cx - w / 2, cy + size * 0.35, size)

    strokes.append(box(30, 40, 130, 90))
    strokes += label("Tablet", 80, 65)
    strokes.append(loop(172, 48, 272, 82, pad=(10, 8), turns=1.03))
    strokes += label("Marginalia", 222, 65)
    strokes.append(diamond(222, 190, 62, 36))
    strokes += label("new ink?", 222, 190)
    strokes.append(box(320, 165, 420, 215))
    strokes += label("Agent", 370, 190)
    strokes += arrow((134, 65), (166, 65))
    strokes += arrow((222, 94), (222, 150))
    strokes += arrow((286, 190), (316, 190))
    strokes += arrow((370, 160), (292, 76))

    ink_page = read_ink(pdf, {0: strokes}).pages[0]
    d = recognise(ink_page.strokes)
    for lab in d.labels():
        cx, cy = (lab.rect[0] + lab.rect[2]) / 2, (lab.rect[1] + lab.rect[3]) / 2
        lab.text = min(labels, key=lambda t: math.dist(labels[t], (cx, cy)))
    render(pdf, 0, strokes, (10, 22, 436, 238), "sketch.png")
    clean = to_svg(d)  # white card: the redraw is black lines, invisible on dark themes
    clean = clean.replace(">", '><rect width="100%" height="100%" fill="#ffffff"/>', 1)
    (OUT / "sketch.svg").write_text(clean)
    (OUT / "sketch.mmd").write_text(to_mermaid(d) + "\n")
    dump("sketch.json", summary(d))
    return d


if __name__ == "__main__":
    print(json.dumps(review_panel(), indent=1))
    print(json.dumps(form_panel(), indent=1))
    print(json.dumps(inbox_panel(), indent=1))
    d = sketch_panel()
    print(to_mermaid(d))
    print("written to", OUT)
