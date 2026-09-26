"""TRMNL client tests (ported from trmnl-mcp)."""

import json
import time
from pathlib import Path

import pytest

from remarkable_mcp.trmnl.client import (
    USER_AGENT,
    ContentError,
    HttpResponse,
    PayloadTooLarge,
    PushLog,
    RateLimited,
    TrmnlClient,
    TrmnlError,
    build_slot_text,
    render_preview,
    sanitize_line,
    slot_key,
)
from remarkable_mcp.trmnl.config import Config


class FakeHttp:
    def __init__(self, responses=None):
        self.calls = []
        self.responses = list(responses or [])
        self.stored = {"message1": "old"}

    def __call__(self, method, url, body, headers):
        self.calls.append((method, url, body, headers))
        if self.responses:
            return self.responses.pop(0)
        if method == "GET":
            return HttpResponse(200, json.dumps({"merge_variables": self.stored}))
        return HttpResponse(200, "{}")


@pytest.fixture
def client(tmp_path):
    cfg = Config(
        plugin_uuid="abc", image_plugin_uuid="img", state_dir=tmp_path, rate_limit_per_hour=3
    )
    http = FakeHttp()
    return TrmnlClient(cfg, http=http), http


# ---- content rules


def test_sanitize_strips_emoji_keeps_umlauts_and_degree():
    assert sanitize_line("⚠️ Regen bis 14h ✅  9–13°C ü") == "Regen bis 14h 9–13°C ü"


def test_build_slot_text_truncates_and_limits_lines():
    text = build_slot_text(["a" * 60, "b", "c", "d"])
    lines = text.split("<br>")
    assert len(lines) == 3
    assert lines[0] == "a" * 42 + "..."
    assert lines[1:] == ["b", "c"]


def test_build_slot_text_strict_raises():
    with pytest.raises(ContentError):
        build_slot_text(["a" * 60], strict=True)
    with pytest.raises(ContentError):
        build_slot_text(["1", "2", "3", "4"], strict=True)


def test_build_slot_text_accepts_string_with_newlines_and_br():
    assert build_slot_text("x\ny<br>z") == "x<br>y<br>z"
    assert build_slot_text("") == ""


def test_slot_key_variants():
    assert slot_key(1) == "message1"
    assert slot_key("6") == "message6"
    assert slot_key("message3") == "message3"
    for bad in (0, 7, "x", "message9"):
        with pytest.raises(ContentError):
            slot_key(bad)


def test_render_preview_lists_all_slots():
    out = render_preview({"message1": "a<br>b", "title": "t"})
    assert "[1] a" in out and "[6] (leer)" in out
    assert '"title"' in out


# ---- client


def test_get_returns_merge_variables(client):
    c, http = client
    assert c.get() == {"message1": "old"}
    assert http.calls[0][0] == "GET"
    assert http.calls[0][1].endswith("/custom_plugins/abc")


def test_push_default_and_deep_merge(client):
    c, http = client
    r = c.push({"message2": "hi"}, merge_strategy="deep_merge")
    body = json.loads(http.calls[-1][2])
    assert body == {"merge_variables": {"message2": "hi"}, "merge_strategy": "deep_merge"}
    assert r["status"] == "ok" and r["pushes_left_this_hour"] == 2
    c.push({"title": "x"})
    assert json.loads(http.calls[-1][2]) == {"merge_variables": {"title": "x"}}


def test_stream_limit_requires_stream(client):
    c, _ = client
    with pytest.raises(ContentError):
        c.encode_payload({"a": [1]}, "deep_merge", stream_limit=5)
    body = json.loads(c.encode_payload({"a": [1]}, "stream", stream_limit=5))
    assert body["stream_limit"] == 5


def test_payload_too_large(client):
    c, _ = client
    with pytest.raises(PayloadTooLarge):
        c.encode_payload({"message1": "x" * 3000})


def test_local_rate_limit_blocks_and_force_bypasses(client):
    c, http = client
    for _ in range(3):
        c.push({"a": 1})
    with pytest.raises(RateLimited):
        c.push({"a": 1})
    assert len([x for x in http.calls if x[0] == "POST"]) == 3
    c.push({"a": 1}, force=True)
    assert len([x for x in http.calls if x[0] == "POST"]) == 4


def test_server_429_surfaces_as_rate_limited(tmp_path):
    cfg = Config(plugin_uuid="abc", state_dir=tmp_path)
    http = FakeHttp([HttpResponse(429, "")])
    with pytest.raises(RateLimited):
        TrmnlClient(cfg, http=http).push({"a": 1})


def test_422_error_message(tmp_path):
    cfg = Config(plugin_uuid="abc", state_dir=tmp_path)
    http = FakeHttp([HttpResponse(422, '{"error":"bad"}')])
    with pytest.raises(TrmnlError, match="422.*bad"):
        TrmnlClient(cfg, http=http).push({"a": 1})


def test_pushlog_prunes_old_entries(tmp_path):
    log = PushLog(tmp_path, 2)
    tmp_path.joinpath("pushes.json").write_text(json.dumps([time.time() - 7200, time.time() - 10]))
    assert log.remaining() == 1
    assert log.seconds_until_slot() == 0


# ---- images


def test_push_image_converts_large_png(client, tmp_path):
    from PIL import Image

    c, http = client
    p = tmp_path / "big.png"
    Image.effect_noise((1600, 1200), 80).convert("RGB").save(p)
    assert p.stat().st_size > 90 * 1024
    r = c.push_image(p)
    assert r["content_type"] == "image/png" and r["bytes"] <= 90 * 1024
    assert "converted" in r["note"]
    sent = http.calls[-1]
    assert sent[1].endswith("/plugin_settings/img/image")
    assert Image.open(__import__("io").BytesIO(sent[2])).size == (800, 480)


def test_push_image_sends_display_sized_png_as_is(client, tmp_path):
    from PIL import Image

    c, http = client
    p = tmp_path / "ok.png"
    Image.new("1", (800, 480), 1).save(p)
    r = c.push_image(p)
    assert r["note"] == "sent as-is"
    assert http.calls[-1][2] == p.read_bytes()


def test_push_image_without_uuid(tmp_path):
    cfg = Config(plugin_uuid="abc", state_dir=tmp_path)
    with pytest.raises(TrmnlError, match="image_plugin_uuid"):
        TrmnlClient(cfg, http=FakeHttp()).push_image(Path(__file__))


def test_urllib_transport_sends_custom_user_agent(monkeypatch):
    """Cloudflare in front of trmnl.com blocks Python-urllib/* with a 403; we must not send it."""
    import urllib.request

    from remarkable_mcp.trmnl.client import _urllib_http

    seen = {}

    class Resp:
        status = 200

        def read(self):
            return b"{}"

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    def fake_urlopen(req, timeout=0):
        seen["ua"] = req.get_header("User-agent")
        seen["ct"] = req.get_header("Content-type")
        return Resp()

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    _urllib_http("POST", "https://trmnl.com/x", b"{}", {"Content-Type": "application/json"})
    assert seen["ua"] == USER_AGENT and USER_AGENT.startswith("remarkable-mcp-trmnl/")
    assert seen["ct"] == "application/json"


def test_timeouts_and_resets_become_trmnl_errors(monkeypatch):
    import urllib.request

    from remarkable_mcp.trmnl import client as client_mod

    for exc in (TimeoutError("read timed out"), ConnectionResetError("reset")):

        def boom(*a, _exc=exc, **k):
            raise _exc

        monkeypatch.setattr(urllib.request, "urlopen", boom)
        with pytest.raises(TrmnlError):
            client_mod._urllib_http("GET", "https://trmnl.com/api/x", None, {})
