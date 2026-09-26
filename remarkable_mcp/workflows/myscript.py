"""MyScript iink batch API: recognise handwriting from strokes, not pixels.

MyScript is the engine behind the reMarkable's own "convert to text". Unlike
image OCR it uses stroke order and geometry, so it is far more accurate on
cursive and on short margin notes. We already have the raw strokes, so no
rendering is needed.

Configuration (both from https://developer.myscript.com, free tier available):

    MYSCRIPT_APPLICATION_KEY   application key
    MYSCRIPT_HMAC_KEY          HMAC key
    MYSCRIPT_LANGUAGE          recognition language, default "en_US" (e.g. "de_DE")

Requests are signed with HMAC-SHA512 over the JSON body, keyed with
application key + HMAC key - the same scheme and payload shape MyScript's own
iinkJS client uses for ``/api/v4.0/iink/batch``.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
from typing import Dict, List, Optional, Sequence

from remarkable_mcp.workflows.ink import Stroke

BATCH_URL = "https://cloud.myscript.com/api/v4.0/iink/batch"
# Stroke coordinates are PDF points; the API wants a DPI to scale its models.
_POINTS_DPI = 72


def configured() -> bool:
    return bool(os.environ.get("MYSCRIPT_APPLICATION_KEY") and os.environ.get("MYSCRIPT_HMAC_KEY"))


def build_request(
    strokes: Sequence[Stroke], language: Optional[str] = None, content_type: str = "Text"
) -> Dict[str, object]:
    """The batch request body for a group of strokes (in drawing order).

    content_type "Text" returns plain text; "Math" returns LaTeX.
    """
    ordered = sorted(strokes, key=lambda s: s.index)
    t = 0
    payload_strokes: List[Dict[str, List[float]]] = []
    for s in ordered:
        xs = [round(x, 2) for x, _ in s.points]
        ys = [round(y, 2) for _, y in s.points]
        # Synthesised timestamps (ms): strokes carry no absolute time, but the
        # recogniser uses relative order and pacing.
        ts = [t + 8 * i for i in range(len(s.points))]
        t = ts[-1] + 120 if ts else t
        payload_strokes.append({"x": xs, "y": ys, "t": ts, "pointerType": "PEN"})
    configuration: Dict[str, object] = {
        "lang": language or os.environ.get("MYSCRIPT_LANGUAGE", "en_US"),
        "export": {"jiix": {"strokes": False, "bounding-box": False}},
    }
    if content_type == "Math":
        configuration["math"] = {"solver": {"enable": False}}
    else:
        configuration["text"] = {"guides": {"enable": False}, "smartGuide": False}
    return {
        "configuration": configuration,
        "xDPI": _POINTS_DPI,
        "yDPI": _POINTS_DPI,
        "contentType": content_type,
        "strokeGroups": [{"strokes": payload_strokes}],
    }


def sign(body: bytes, application_key: str, hmac_key: str) -> str:
    return hmac.new((application_key + hmac_key).encode(), body, hashlib.sha512).hexdigest()


def recognise_text(strokes: Sequence[Stroke], timeout: float = 30.0) -> Optional[str]:
    """Plain-text transcription of the strokes, or None when not configured."""
    return _recognise(strokes, "Text", "text/plain", timeout)


def recognise_math(strokes: Sequence[Stroke], timeout: float = 30.0) -> Optional[str]:
    """LaTeX for handwritten math, or None when not configured."""
    return _recognise(strokes, "Math", "application/x-latex", timeout)


def _recognise(
    strokes: Sequence[Stroke], content_type: str, mime: str, timeout: float
) -> Optional[str]:
    import requests

    if not strokes or not configured():
        return None
    app_key = os.environ["MYSCRIPT_APPLICATION_KEY"]
    body = json.dumps(build_request(strokes, content_type=content_type), separators=(",", ":"))
    body = body.encode()
    resp = requests.post(
        BATCH_URL,
        data=body,
        headers={
            "Accept": f"application/json,{mime}",
            "Content-Type": "application/json",
            "applicationKey": app_key,
            "hmac": sign(body, app_key, os.environ["MYSCRIPT_HMAC_KEY"]),
        },
        timeout=timeout,
    )
    resp.raise_for_status()
    ctype = resp.headers.get("Content-Type", "")
    if "json" in ctype:
        data = resp.json()
        text = data.get("label") or data.get("text") or ""
    else:
        text = resp.text
    text = text.strip()
    return text or None
