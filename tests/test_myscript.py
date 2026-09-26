"""Tests for the MyScript stroke-recognition client (no network)."""

import hashlib
import hmac
import json

from remarkable_mcp.workflows.ink import myscript
from remarkable_mcp.workflows.ink.page import Stroke


def _strokes():
    return [
        Stroke(index=2, points=[(10, 10), (12, 14)], tool="fineliner", color="black", width=1),
        Stroke(index=1, points=[(0, 0), (1, 1), (2, 0)], tool="fineliner", color="black", width=1),
    ]


def test_request_shape_orders_strokes_and_times_them():
    body = myscript.build_request(_strokes(), language="de_DE")
    assert body["contentType"] == "Text"
    assert body["configuration"]["lang"] == "de_DE"
    [group] = body["strokeGroups"]
    first, second = group["strokes"]
    assert first["x"] == [0, 1, 2] and first["pointerType"] == "PEN"
    assert second["t"][0] > first["t"][-1]  # later stroke, later time


def test_signature_matches_iinkjs_scheme():
    body = b'{"a":1}'
    expected = hmac.new(b"appkeyhmackey", body, hashlib.sha512).hexdigest()
    assert myscript.sign(body, "appkey", "hmackey") == expected


def test_recognise_posts_signed_body(monkeypatch):
    monkeypatch.setenv("MYSCRIPT_APPLICATION_KEY", "app")
    monkeypatch.setenv("MYSCRIPT_HMAC_KEY", "secret")
    sent = {}

    class Resp:
        headers = {"Content-Type": "text/plain"}
        text = " hello world \n"

        def raise_for_status(self):
            pass

    def fake_post(url, data, headers, timeout):
        sent.update(url=url, data=data, headers=headers)
        return Resp()

    monkeypatch.setattr("requests.post", fake_post)
    assert myscript.recognise_text(_strokes()) == "hello world"
    assert sent["url"].endswith("/api/v4.0/iink/batch")
    assert sent["headers"]["applicationKey"] == "app"
    assert sent["headers"]["hmac"] == myscript.sign(sent["data"], "app", "secret")
    assert json.loads(sent["data"])["contentType"] == "Text"


def test_not_configured(monkeypatch):
    monkeypatch.delenv("MYSCRIPT_APPLICATION_KEY", raising=False)
    assert not myscript.configured()
    assert myscript.recognise_text(_strokes()) is None


def test_handwriting_pipeline_prefers_strokes(monkeypatch, tmp_path):
    from remarkable_mcp.workflows.ink import handwriting

    monkeypatch.setenv("REMARKABLE_WORKFLOW_STATE", str(tmp_path))
    monkeypatch.delenv("REMARKABLE_HANDWRITING_BACKEND", raising=False)
    monkeypatch.delenv("GOOGLE_VISION_API_KEY", raising=False)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setenv("MYSCRIPT_APPLICATION_KEY", "app")
    monkeypatch.setenv("MYSCRIPT_HMAC_KEY", "secret")
    calls = []
    monkeypatch.setattr(myscript, "recognise_text", lambda s: calls.append(len(s)) or "tick")
    assert handwriting.backend() == "myscript"
    strokes = _strokes()
    png = handwriting.render_strokes_png(strokes)
    assert handwriting.transcribe_many([png], strokes=[strokes]) == [("tick", "myscript")]
    # Cached by stroke fingerprints: no second API call.
    assert handwriting.transcribe(png, strokes=strokes) == ("tick", "myscript")
    assert calls == [2]
    # No strokes and no image engine configured: nothing to do.
    assert handwriting.transcribe(png) == (None, "none")
