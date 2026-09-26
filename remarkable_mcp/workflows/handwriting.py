"""Crop handwriting regions to images and (optionally) transcribe them.

Crops are rendered from the stroke geometry itself (black ink on white, no
page underneath), which is what handwriting recognisers do best on and keeps
the image small enough for a small vision model.

Transcription backends, chosen by ``REMARKABLE_HANDWRITING_BACKEND``:

- ``auto`` (default): myscript if its keys are set, else google if
  ``GOOGLE_VISION_API_KEY`` is set, else claude if ``ANTHROPIC_API_KEY`` is
  set, else none
- ``myscript``: MyScript iink - recognises the *strokes* (order and geometry),
  the most accurate option for handwriting; see ``workflows.myscript``
- ``google``: Google Cloud Vision DOCUMENT_TEXT_DETECTION
- ``claude``: Anthropic Messages API with a vision model
  (``REMARKABLE_HANDWRITING_MODEL``, default ``claude-haiku-4-5``)
- ``tesseract``: local Tesseract (poor on handwriting; last resort)
- ``none``: never transcribe; tools return the crop images instead

Transcriptions are cached by stroke fingerprint, so re-collecting the same
marks costs nothing.
"""

from __future__ import annotations

import base64
import hashlib
import io
import logging
import os
import tempfile
from pathlib import Path
from typing import List, Optional, Sequence, Tuple

from remarkable_mcp.workflows.ink import Rect, Stroke
from remarkable_mcp.workflows.state import Store

logger = logging.getLogger(__name__)

_DEFAULT_CLAUDE_MODEL = "claude-haiku-4-5"
_PROMPT = (
    "Transcribe the handwriting in this image exactly. It is a reviewer's note "
    "on a document draft. Keep line breaks. Keep symbols like ?, !, arrows (write ->), "
    "and carets (write ^). Do not add commentary. If a word is illegible write [?]."
)


def backend() -> str:
    choice = os.environ.get("REMARKABLE_HANDWRITING_BACKEND", "auto").strip().lower()
    if choice != "auto":
        return choice
    from remarkable_mcp.workflows import myscript

    if myscript.configured():
        return "myscript"
    if os.environ.get("GOOGLE_VISION_API_KEY"):
        return "google"
    if os.environ.get("ANTHROPIC_API_KEY"):
        return "claude"
    return "none"


def render_strokes_png(
    strokes: Sequence[Stroke], rect: Optional[Rect] = None, scale: float = 3.0, pad: float = 6.0
) -> bytes:
    """Draw strokes (PDF points) black-on-white, cropped to ``rect`` (+padding)."""
    from PIL import Image, ImageDraw

    if rect is None:
        xs = [x for s in strokes for x, _ in s.points]
        ys = [y for s in strokes for _, y in s.points]
        rect = (min(xs), min(ys), max(xs), max(ys))
    x0, y0 = rect[0] - pad, rect[1] - pad
    w = max(1, int((rect[2] - rect[0] + 2 * pad) * scale))
    h = max(1, int((rect[3] - rect[1] + 2 * pad) * scale))
    img = Image.new("L", (w, h), 255)
    draw = ImageDraw.Draw(img)
    line_w = max(2, int(round(0.9 * scale)))
    for s in strokes:
        pts = [((x - x0) * scale, (y - y0) * scale) for x, y in s.points]
        if len(pts) == 1:
            draw.point(pts[0], fill=0)
        else:
            draw.line(pts, fill=0, width=line_w, joint="curve")
    buf = io.BytesIO()
    img.save(buf, format="PNG", optimize=True)
    return buf.getvalue()


def render_page_region_png(
    pdf_bytes: Optional[bytes],
    pdf_page: Optional[int],
    strokes: Sequence[Stroke],
    rect: Rect,
    scale: float = 3.0,
    pad: float = 10.0,
) -> bytes:
    """Region of the page with the PDF underneath and the ink on top (for context)."""
    from PIL import Image, ImageDraw

    x0, y0 = max(0.0, rect[0] - pad), max(0.0, rect[1] - pad)
    x1, y1 = rect[2] + pad, rect[3] + pad
    w, h = max(1, int((x1 - x0) * scale)), max(1, int((y1 - y0) * scale))
    base = Image.new("RGB", (w, h), "white")
    if pdf_bytes is not None and pdf_page is not None:
        import fitz

        with fitz.open(stream=pdf_bytes, filetype="pdf") as doc:
            pix = doc[pdf_page].get_pixmap(
                matrix=fitz.Matrix(scale, scale), clip=fitz.Rect(x0, y0, x1, y1)
            )
            base = Image.frombytes("RGB", (pix.width, pix.height), pix.samples)
    draw = ImageDraw.Draw(base)
    for s in strokes:
        pts = [((x - x0) * scale, (y - y0) * scale) for x, y in s.points]
        if s.is_highlighter:
            continue
        colour = (200, 0, 0) if s.color in ("black", "gray") else (0, 0, 200)
        if len(pts) > 1:
            draw.line(pts, fill=colour, width=max(2, int(scale)), joint="curve")
    buf = io.BytesIO()
    base.save(buf, format="PNG", optimize=True)
    return buf.getvalue()


def _cache_key(png: bytes, engine: str) -> str:
    variant = engine
    if engine == "claude":
        variant += ":" + os.environ.get("REMARKABLE_HANDWRITING_MODEL", _DEFAULT_CLAUDE_MODEL)
    elif engine == "myscript":
        variant += ":" + os.environ.get("MYSCRIPT_LANGUAGE", "en_US")
    digest = hashlib.sha1(variant.encode() + b"\0" + png).hexdigest()[:24]
    return f"{engine}-{digest}"


def _strokes_key(strokes: Sequence[Stroke]) -> bytes:
    return "|".join(s.fingerprint() for s in sorted(strokes, key=lambda s: s.index)).encode()


def transcribe(
    png: bytes, engine: Optional[str] = None, strokes: Optional[Sequence[Stroke]] = None
) -> Tuple[Optional[str], str]:
    """Return (text or None, engine used). Never raises.

    Stroke-based engines (myscript) use ``strokes``; image engines use ``png``.
    Without strokes, myscript falls back to the next image engine available.
    """
    engine = engine or backend()
    if engine == "myscript" and not strokes:
        engine = _image_fallback()
    if engine == "none":
        return None, engine
    cache = Store("handwriting-cache")
    key = _cache_key(_strokes_key(strokes) if engine == "myscript" else png, engine)
    try:
        hit = cache.get(key)
    except Exception:
        hit = None
    if hit is not None:
        return hit.get("text"), engine
    try:
        if engine == "myscript":
            from remarkable_mcp.workflows import myscript

            text = myscript.recognise_text(strokes)
        elif engine == "google":
            text = _google(png)
        elif engine == "claude":
            text = _claude(png)
        elif engine == "tesseract":
            text = _tesseract(png)
        else:
            logger.warning("Unknown handwriting backend %r", engine)
            return None, "none"
    except Exception as exc:  # best effort: the crop is still returned
        logger.warning("Handwriting transcription failed (%s): %s", engine, exc)
        return None, engine
    if text is not None:
        try:
            cache.put(key, {"text": text, "engine": engine})
        except OSError:
            pass
    return text, engine


def _image_fallback() -> str:
    if os.environ.get("GOOGLE_VISION_API_KEY"):
        return "google"
    if os.environ.get("ANTHROPIC_API_KEY"):
        return "claude"
    return "none"


def transcribe_many(
    pngs: Sequence[bytes],
    engine: Optional[str] = None,
    deadline: float = 50.0,
    strokes: Optional[Sequence[Sequence[Stroke]]] = None,
) -> List[Tuple[Optional[str], str]]:
    """Transcribe crops in parallel; crops not done by ``deadline`` seconds get None.

    Keeps one collect call well inside typical MCP client timeouts even with
    many notes; unfinished crops are simply returned untranscribed (and will be
    cached for the next call once they complete).
    """
    import concurrent.futures as cf

    engine = engine or backend()
    if engine == "none" or not pngs:
        return [(None, engine) for _ in pngs]
    results: List[Tuple[Optional[str], str]] = [(None, engine) for _ in pngs]
    pool = cf.ThreadPoolExecutor(max_workers=4)
    groups = list(strokes) if strokes is not None else [None] * len(pngs)
    futures = {
        pool.submit(transcribe, png, engine, group): i
        for i, (png, group) in enumerate(zip(pngs, groups))
    }
    try:
        for fut in cf.as_completed(futures, timeout=deadline):
            results[futures[fut]] = fut.result()
    except cf.TimeoutError:
        logger.warning("Handwriting transcription deadline hit; returning partial results")
    finally:
        pool.shutdown(wait=False, cancel_futures=True)
    return results


def _google(png: bytes) -> Optional[str]:
    import requests

    key = os.environ["GOOGLE_VISION_API_KEY"]
    resp = requests.post(
        f"https://vision.googleapis.com/v1/images:annotate?key={key}",
        json={
            "requests": [
                {
                    "image": {"content": base64.b64encode(png).decode()},
                    "features": [{"type": "DOCUMENT_TEXT_DETECTION"}],
                    "imageContext": {"languageHints": ["en", "de"]},
                }
            ]
        },
        timeout=60,
    )
    resp.raise_for_status()
    data = resp.json()["responses"][0]
    text = data.get("fullTextAnnotation", {}).get("text", "").strip()
    return text or None


def _claude(png: bytes) -> Optional[str]:
    import requests

    resp = requests.post(
        "https://api.anthropic.com/v1/messages",
        headers={
            "x-api-key": os.environ["ANTHROPIC_API_KEY"],
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        },
        json={
            "model": os.environ.get("REMARKABLE_HANDWRITING_MODEL", _DEFAULT_CLAUDE_MODEL),
            "max_tokens": 400,
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "image",
                            "source": {
                                "type": "base64",
                                "media_type": "image/png",
                                "data": base64.b64encode(png).decode(),
                            },
                        },
                        {"type": "text", "text": _PROMPT},
                    ],
                }
            ],
        },
        timeout=60,
    )
    resp.raise_for_status()
    parts = [b.get("text", "") for b in resp.json().get("content", []) if b.get("type") == "text"]
    text = "".join(parts).strip()
    return text or None


def _tesseract(png: bytes) -> Optional[str]:
    import pytesseract
    from PIL import Image

    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "crop.png"
        path.write_bytes(png)
        text = pytesseract.image_to_string(Image.open(path), config="--psm 6 --oem 3")
    return text.strip() or None
