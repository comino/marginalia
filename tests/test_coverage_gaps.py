"""Tests for code paths the feature tests did not reach (found with coverage)."""

import asyncio
import json

import pymupdf
import pytest

from test_sketch import arrow_in_stroke, ellipse_path, line_path, rect_path
from test_workflows import (  # noqa: F401
    DRAFT,
    FINELINER,
    _fake_path,
    _handwriting,
    _json_of,
    _page_w,
    _phrase_rects,
    _strike,
    cloud,
)


@pytest.fixture
def by_name(monkeypatch):
    monkeypatch.setattr(
        "remarkable_mcp.tools._find_target_document",
        lambda items, by_id, name: next((d for d in items if d.VissibleName == name), None),
    )


def _blank(cloud, name, pages=1):  # noqa: F811
    doc = pymupdf.open()
    for _ in range(pages):
        doc.new_page(width=446, height=595)
    return cloud.upload_document(doc.tobytes(), name, "pdf")


# --------------------------------------------------------------------------- live analysis


def test_live_analyse_annotations_on_a_pdf(cloud):  # noqa: F811
    from remarkable_mcp.workflows import live_tools
    from remarkable_mcp.workflows.review_pdf import render_review_pdf

    r = render_review_pdf(DRAFT)
    doc = cloud.upload_document(r.pdf, "Draft", "pdf")
    pno, rects = _phrase_rects(r.pdf, "permission to fetch data")
    cloud.annotate(doc.id, {pno: [(_strike(rects), FINELINER, _page_w(r.pdf))]})
    page_id = cloud.page_ids[doc.id][pno]
    pages, images = live_tools._analyse(doc.id, [page_id], "auto", include_images=True)
    [page] = pages
    assert page["page"] == pno + 1 and page["marks"][0]["kind"] == "strikethrough"
    assert images and images[0][2].startswith(b"\x89PNG")


def test_live_analyse_sketch_on_a_blank_page(cloud):  # noqa: F811
    from remarkable_mcp.workflows import live_tools

    doc = _blank(cloud, "Sketch")
    shapes = [
        rect_path(50, 50, 150, 100),
        ellipse_path(310, 75, 50, 25),
        arrow_in_stroke((152, 75), (258, 75)),
    ]
    cloud.annotate(doc.id, {0: [(p, FINELINER, 446.0) for p in shapes]})
    pages, _ = live_tools._analyse(doc.id, [cloud.page_ids[doc.id][0]], "auto", False)
    diagram = pages[0]["diagram"]
    assert len(diagram["nodes"]) == 2 and diagram["mermaid"].startswith("flowchart")


def test_live_analyse_unknown_document(cloud):  # noqa: F811
    from remarkable_mcp.workflows import live_tools

    assert live_tools._analyse("missing", ["p"], "auto", False) == ([], [])


def test_live_status_reports_watcher_state(monkeypatch):
    from remarkable_mcp.workflows import live, live_tools

    monkeypatch.setattr(live, "_shared", None)
    assert json.loads(asyncio.run(live_tools.remarkable_live_status()))["running"] is False
    w = live.Watcher(take_snapshot=lambda: {}, connect=None)
    w.mode, w.state, w.errors_total, w.last_error = "socket", {}, 2, "HTTP 429"
    monkeypatch.setattr(live, "_shared", w)
    out = json.loads(asyncio.run(live_tools.remarkable_live_status()))
    assert out["mode"] == "socket" and out["refresh_errors"] == 2


# --------------------------------------------------------------------------- structure tools


def test_table_tool(cloud, by_name):  # noqa: F811
    from remarkable_mcp.workflows import structure_tools as st

    doc = _blank(cloud, "Table")
    lines = []
    for r in range(4):
        lines.append(line_path((40, 60 + r * 30), (310, 60.5 + r * 30)))
    for c in range(4):
        lines.append(line_path((40 + c * 90, 60), (40.5 + c * 90, 150)))
    cells = _handwriting(50, 70, words=1, word_w=30) + _handwriting(140, 100, words=1, word_w=30)
    cloud.annotate(doc.id, {0: [(p, FINELINER, 446.0) for p in lines + cells]})
    got = asyncio.run(st.remarkable_table("Table", include_images=True))
    data = json.loads(got[0].text)
    assert (data["rows"], data["columns"]) == (3, 3)
    assert data["markdown"].startswith("|") and "untranscribed" not in data
    assert sum(1 for b in got if getattr(b, "type", "") == "image") == 2


def test_table_tool_without_a_table(cloud, by_name):  # noqa: F811
    from remarkable_mcp.workflows import structure_tools as st

    doc = _blank(cloud, "Plain")
    cloud.annotate(doc.id, {0: [(p, FINELINER, 446.0) for p in _handwriting(40, 60, words=3)]})
    assert _json_of(asyncio.run(st.remarkable_table("Plain")))["table"] is None


def test_wireframe_tool_with_preview(cloud, by_name):  # noqa: F811
    from remarkable_mcp.workflows import structure_tools as st

    doc = _blank(cloud, "Wire")
    strokes = [
        rect_path(20, 20, 400, 300),
        rect_path(220, 60, 380, 84),
        rect_path(220, 100, 300, 126),
    ]
    cloud.annotate(doc.id, {0: [(p, FINELINER, 446.0) for p in strokes]})
    got = asyncio.run(st.remarkable_wireframe("Wire", include_images=True))
    data = json.loads(got[0].text if isinstance(got, list) else got)
    assert {e["role"] for e in data["elements"]} >= {"container", "input"}
    assert "<html>" in data["html"]


@pytest.mark.parametrize(
    "call,error",
    [
        (lambda st: st.remarkable_table("Nope"), "document_not_found"),
        (lambda st: st.remarkable_table("Blank", page=9), "page_out_of_range"),
        (lambda st: st.remarkable_wireframe("Blank", region=[1, 2, 3]), "invalid_arguments"),
        (lambda st: st.remarkable_math("Nope"), "document_not_found"),
    ],
)
def test_structure_tool_errors(cloud, by_name, call, error):  # noqa: F811
    from remarkable_mcp.workflows import structure_tools as st

    _blank(cloud, "Blank")
    assert _json_of(asyncio.run(call(st)))["_error"]["type"] == error


# --------------------------------------------------------------------------- autopilot loop + CLI


def test_autopilot_loop_checks_after_debounce_and_restarts_watcher(monkeypatch):
    from remarkable_mcp.workflows import autopilot, live

    runs = {"n": 0}

    class FakeWatcher:
        def __init__(self, min_refresh=None):
            self.q = asyncio.Queue()

        def subscribe(self):
            return self.q

        async def run(self):
            runs["n"] += 1
            if runs["n"] == 1:
                raise RuntimeError("first watcher dies")
            await asyncio.sleep(3600)

    monkeypatch.setattr(live, "Watcher", FakeWatcher)
    pilot = autopilot.Autopilot(
        {**autopilot.DEFAULTS, "debounce_seconds": 0.05, "trmnl_slot": None}
    )
    checks = []

    async def fake_check():
        checks.append(1)
        return {"attention": []}

    monkeypatch.setattr(pilot, "check", fake_check)

    async def scenario():
        task = asyncio.create_task(pilot.run())
        # the queued ink change is debounced and checked; the loop also
        # notices that the first watcher died and restarts it
        await asyncio.sleep(0.5)
        task.cancel()

    # Deliver one change through the watcher's queue once the loop is waiting.
    orig_init = FakeWatcher.__init__

    def init(self, min_refresh=None):
        orig_init(self, min_refresh)
        self.q.put_nowait(live.Change("a", "A", "/A", "ink", ["p"]))

    monkeypatch.setattr(FakeWatcher, "__init__", init)
    asyncio.run(scenario())
    assert len(checks) >= 2  # startup check + after the debounced change
    assert runs["n"] >= 2  # the dead watcher was restarted


def test_autopilot_once_cli(monkeypatch, capsys):
    from remarkable_mcp.workflows import autopilot

    async def fake_overview(self):
        return {"attention": [], "waiting": 1}

    monkeypatch.setattr(autopilot.Autopilot, "overview", fake_overview)
    autopilot.main(["--once"])
    out = json.loads(capsys.readouterr().out)
    assert out["trmnl"] == ["REMARKABLE: nichts Neues", "1 warten auf dich"]
    assert out["agent"] is False


def test_autopilot_config_file(tmp_path, monkeypatch):
    from remarkable_mcp.workflows import autopilot

    cfg = tmp_path / "ap.json"
    cfg.write_text(json.dumps({"trmnl_slot": None, "debounce_seconds": 5}))
    monkeypatch.setenv("REMARKABLE_AUTOPILOT_CONFIG", str(cfg))
    loaded = autopilot.load_config()
    assert loaded["trmnl_slot"] is None and loaded["debounce_seconds"] == 5
    assert loaded["min_agent_interval_seconds"] == autopilot.DEFAULTS["min_agent_interval_seconds"]


# --------------------------------------------------------------------------- handwriting backends


class _Resp:
    def __init__(self, payload):
        self.payload = payload

    def raise_for_status(self):
        pass

    def json(self):
        return self.payload


def test_google_backend_request_and_parse(monkeypatch):
    from remarkable_mcp.workflows import handwriting

    monkeypatch.setenv("GOOGLE_VISION_API_KEY", "k")
    sent = {}

    def post(url, json=None, timeout=None, **kw):
        sent.update(url=url, body=json)
        return _Resp({"responses": [{"fullTextAnnotation": {"text": " delete this\n"}}]})

    monkeypatch.setattr("requests.post", post)
    assert handwriting.transcribe(b"png", "google") == ("delete this", "google")
    req = sent["body"]["requests"][0]
    assert req["features"][0]["type"] == "DOCUMENT_TEXT_DETECTION"
    assert "de" in req["imageContext"]["languageHints"] and sent["url"].endswith("key=k")


def test_claude_backend_request_and_parse(monkeypatch):
    from remarkable_mcp.workflows import handwriting

    monkeypatch.setenv("ANTHROPIC_API_KEY", "k")
    monkeypatch.setenv("REMARKABLE_HANDWRITING_MODEL", "claude-haiku-4-5")
    sent = {}

    def post(url, headers=None, json=None, timeout=None, **kw):
        sent.update(url=url, headers=headers, body=json)
        return _Resp({"content": [{"type": "text", "text": "\\frac{a}{b}"}]})

    monkeypatch.setattr("requests.post", post)
    text, engine = handwriting.transcribe(b"png-bytes", "claude", mode="math")
    assert (text, engine) == ("\\frac{a}{b}", "claude")
    assert sent["url"] == "https://api.anthropic.com/v1/messages"
    assert sent["headers"]["x-api-key"] == "k" and sent["headers"]["anthropic-version"]
    content = sent["body"]["messages"][0]["content"]
    assert content[0]["type"] == "image" and "LaTeX" in content[1]["text"]
    assert sent["body"]["model"] == "claude-haiku-4-5"


def test_backend_errors_degrade_to_none(monkeypatch):
    from remarkable_mcp.workflows import handwriting

    monkeypatch.setenv("GOOGLE_VISION_API_KEY", "k")

    def boom(*a, **k):
        raise ConnectionError("offline")

    monkeypatch.setattr("requests.post", boom)
    assert handwriting.transcribe(b"x", "google") == (None, "google")


def test_math_mode_skips_plain_ocr_engines(monkeypatch):
    from remarkable_mcp.workflows import handwriting

    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    assert handwriting.transcribe(b"x", "google", mode="math") == (None, "none")


def test_transcribe_many_respects_the_deadline(monkeypatch):
    import time as _t

    from remarkable_mcp.workflows import handwriting

    def slow(png, engine=None, strokes=None, mode="text"):
        _t.sleep(2)
        return "late", engine

    monkeypatch.setattr(handwriting, "transcribe", slow)
    t0 = _t.time()
    out = handwriting.transcribe_many([b"a", b"b"], "google", deadline=0.3)
    assert _t.time() - t0 < 1.5
    assert out == [(None, "google"), (None, "google")]


# --------------------------------------------------------------------------- TRMNL config


def test_trmnl_config_resolution(tmp_path, monkeypatch):
    from remarkable_mcp.trmnl.config import ConfigError, load_config

    path = tmp_path / "trmnl.json"
    monkeypatch.setenv("TRMNL_CONFIG", str(path))
    with pytest.raises(ConfigError):
        load_config()
    path.write_text(json.dumps({"plugin_uuid": "from-file", "rate_limit_per_hour": 6}))
    cfg = load_config()
    assert cfg.plugin_uuid == "from-file" and cfg.rate_limit_per_hour == 6
    monkeypatch.setenv("TRMNL_PLUGIN_UUID", "from-env")
    assert load_config().plugin_uuid == "from-env"
    path.write_text("{not json")
    with pytest.raises(ConfigError):
        load_config()


# --------------------------------------------------------------------------- prompts


def test_all_prompts_render_with_arguments():
    from remarkable_mcp.server import mcp

    for p in asyncio.run(mcp.list_prompts()):
        args = {a.name: "x" for a in (p.arguments or []) if a.required}
        result = asyncio.run(mcp.get_prompt(p.name, args))
        text = "".join(getattr(m.content, "text", "") for m in result.messages)
        assert len(text) > 40, p.name


# --------------------------------------------------------------------------- TRMNL tool errors


def test_trmnl_tools_report_config_errors_as_text(monkeypatch, tmp_path):
    from remarkable_mcp.trmnl import tools as t

    monkeypatch.setattr(t, "_client", None)
    monkeypatch.setenv("TRMNL_CONFIG", str(tmp_path / "missing.json"))
    for call in (
        t.trmnl_status,
        t.trmnl_get,
        t.trmnl_dashboard,
        lambda: t.trmnl_clear(),
        lambda: t.trmnl_send("a", "b"),
        lambda: t.trmnl_send_list("a", ["b"]),
        lambda: t.trmnl_set_slots({"1": "x"}),
        lambda: t.trmnl_image("/nope.png"),
    ):
        assert call().startswith("ERROR:")


def test_trmnl_push_validation(monkeypatch, tmp_path):
    from remarkable_mcp.trmnl import tools as t
    from remarkable_mcp.trmnl.client import TrmnlClient
    from remarkable_mcp.trmnl.config import Config
    from test_trmnl_tools import FakeHttp

    monkeypatch.setattr(
        t, "_client", TrmnlClient(Config(plugin_uuid="abc", state_dir=tmp_path), http=FakeHttp())
    )
    assert t.trmnl_push({"a": 1}, merge_strategy="bogus").startswith("ERROR:")
    assert t.trmnl_push({"a": 1}, dry_run=True).startswith("DRY RUN")
    assert t.trmnl_set_slots({}).startswith("ERROR:")
    assert t.trmnl_image("/nope.png").startswith("ERROR:")  # no image plugin configured


# --------------------------------------------------------------------------- form / inbox / sketch edge paths


def test_form_tools_need_cloud_for_sending(cloud, monkeypatch):  # noqa: F811
    from remarkable_mcp.workflows import cloud as cloud_mod
    from remarkable_mcp.workflows import form_tools

    monkeypatch.setattr(cloud_mod, "is_cloud", lambda: False)
    err = _json_of(asyncio.run(form_tools.remarkable_ask("Q?")))
    assert err["_error"]["type"] == "unsupported_transport"


def test_form_read_when_document_deleted(cloud):  # noqa: F811
    from remarkable_mcp.workflows import form_tools

    sent = _json_of(asyncio.run(form_tools.remarkable_ask("Q?")))
    doc_id = form_tools._forms().get(sent["form"])["doc_id"]
    cloud.docs[doc_id].parent = cloud.docs[doc_id].Parent = "trash"
    err = _json_of(asyncio.run(form_tools.remarkable_form_read(sent["form"])))
    assert err["_error"]["type"] == "document_missing"
    listed = _json_of(asyncio.run(form_tools.remarkable_form_list()))
    assert listed["forms"][0]["status"] == "missing"


def test_clarify_on_a_plain_document_and_errors(cloud, by_name):  # noqa: F811
    from remarkable_mcp.workflows import form_tools, tools
    from remarkable_mcp.workflows.review_pdf import render_review_pdf

    r = render_review_pdf(DRAFT)
    doc = cloud.upload_document(r.pdf, "Some PDF", "pdf")
    pno, rects = _phrase_rects(r.pdf, "permission to fetch data")
    cloud.annotate(doc.id, {pno: [(_strike(rects), FINELINER, _page_w(r.pdf))]})
    [mark] = _json_of(asyncio.run(tools.remarkable_annotations("Some PDF")))["marks"]
    sent = _json_of(
        asyncio.run(form_tools.remarkable_clarify(mark["id"], "Why?", document="Some PDF"))
    )
    assert "form" in sent
    assert (
        _json_of(asyncio.run(form_tools.remarkable_clarify("m1", "?")))["_error"]["type"]
        == "invalid_arguments"
    )
    assert (
        _json_of(asyncio.run(form_tools.remarkable_clarify("m1", "?", review="nope")))["_error"][
            "type"
        ]
        == "not_found"
    )


def test_inbox_setup_on_an_existing_notebook(cloud, by_name):  # noqa: F811
    from remarkable_mcp.workflows import inbox_tools

    _blank(cloud, "My notebook")
    got = _json_of(
        asyncio.run(inbox_tools.remarkable_inbox_setup(document="My notebook", name="work"))
    )
    assert got["uploaded"] is False and got["inbox"] == "work"
    err = _json_of(asyncio.run(inbox_tools.remarkable_inbox_setup(document="Nope")))
    assert err["_error"]["type"] == "document_not_found"


def test_sketch_tool_errors_and_empty_page(cloud, by_name):  # noqa: F811
    from remarkable_mcp.workflows import sketch_tools

    _blank(cloud, "Empty")
    assert (
        _json_of(asyncio.run(sketch_tools.remarkable_sketch("Empty", region=[1, 2])))["_error"][
            "type"
        ]
        == "invalid_region"
    )
    assert (
        _json_of(asyncio.run(sketch_tools.remarkable_sketch("Nope")))["_error"]["type"]
        == "document_not_found"
    )
    assert (
        _json_of(asyncio.run(sketch_tools.remarkable_sketch("Empty", page=5)))["_error"]["type"]
        == "page_out_of_range"
    )
    assert _json_of(asyncio.run(sketch_tools.remarkable_sketch("Empty")))["nodes"] == []
    assert _json_of(asyncio.run(sketch_tools.remarkable_regions("Empty")))["regions"] == []


def test_regions_page_out_of_range(cloud, by_name):  # noqa: F811
    from remarkable_mcp.workflows import sketch_tools

    _blank(cloud, "Empty")
    assert (
        _json_of(asyncio.run(sketch_tools.remarkable_regions("Empty", page=5)))["_error"]["type"]
        == "page_out_of_range"
    )
