"""Exercise the MCP layer through the real MCPServer with a fake HTTP backend."""

import json

import pytest
from mcp.server.mcpserver import MCPServer

from remarkable_mcp.trmnl import tools as server
from remarkable_mcp.trmnl.client import HttpResponse, TrmnlClient
from remarkable_mcp.trmnl.config import Config

# The tools are exercised on a private server so the tests do not depend on a
# TRMNL config existing on the machine running them.
_mcp = MCPServer(name="trmnl-test")
server.register(_mcp)
server.mcp = _mcp


class FakeHttp:
    def __init__(self):
        self.stored = {"message1": "WETTER 9°C", "message2": "TRAINING"}
        self.posts = []

    def __call__(self, method, url, body, headers):
        if method == "GET":
            return HttpResponse(200, json.dumps({"merge_variables": self.stored}))
        payload = json.loads(body)
        self.posts.append(payload)
        if payload.get("merge_strategy") == "deep_merge":
            self.stored.update(payload["merge_variables"])
        else:
            self.stored = dict(payload["merge_variables"])
        return HttpResponse(200, "{}")


@pytest.fixture(autouse=True)
def fake_client(tmp_path, monkeypatch):
    http = FakeHttp()
    cfg = Config(plugin_uuid="abc", state_dir=tmp_path, source="test")
    monkeypatch.setattr(server, "_client", TrmnlClient(cfg, http=http))
    return http


def _text(result):
    return "".join(block.text for block in result.content if block.type == "text")


async def test_tools_are_registered():
    names = {t.name for t in await server.mcp.list_tools()}
    assert {
        "trmnl_status",
        "trmnl_get",
        "trmnl_dashboard",
        "trmnl_set_slots",
        "trmnl_set_slot",
        "trmnl_clear_slots",
        "trmnl_send",
        "trmnl_send_list",
        "trmnl_push",
        "trmnl_clear",
        "trmnl_image",
    } <= names


async def test_set_slots_deep_merges_and_sanitizes(fake_client):
    r = await server.mcp.call_tool(
        "trmnl_set_slots", {"slots": {"3": ["GEWICHT 85.3kg ✅", "noch 6.3kg"]}}
    )
    out = json.loads(_text(r))
    assert out["status"] == "ok"
    assert out["updated"] == {"message3": "GEWICHT 85.3kg<br>noch 6.3kg"}
    assert fake_client.posts[-1]["merge_strategy"] == "deep_merge"
    assert fake_client.stored["message1"] == "WETTER 9°C"  # untouched


async def test_set_slots_dry_run_does_not_post(fake_client):
    r = await server.mcp.call_tool("trmnl_set_slots", {"slots": {"1": "neu"}, "dry_run": True})
    assert "DRY RUN" in _text(r) and "[1] neu" in _text(r)
    assert fake_client.posts == []


async def test_set_slots_bad_slot_returns_error_text(fake_client):
    r = await server.mcp.call_tool("trmnl_set_slots", {"slots": {"9": "x"}})
    assert _text(r).startswith("ERROR:")
    assert fake_client.posts == []


async def test_set_slot_and_clear_slots(fake_client):
    await server.mcp.call_tool("trmnl_set_slot", {"slot": 5, "lines": ["a", "b"]})
    assert fake_client.stored["message5"] == "a<br>b"
    r = await server.mcp.call_tool("trmnl_clear_slots", {"slots": [5, 2]})
    assert json.loads(_text(r))["cleared"] == ["message2", "message5"]
    assert fake_client.stored["message5"] == "" and fake_client.stored["message2"] == ""


async def test_send_replaces_everything(fake_client):
    await server.mcp.call_tool("trmnl_send", {"title": "Deploy", "message": "v2 🚀 live"})
    assert fake_client.stored == {"title": "Deploy", "message": "v2 live"}


async def test_push_raw_with_stream(fake_client):
    r = await server.mcp.call_tool(
        "trmnl_push",
        {"merge_variables": {"log": ["x"]}, "merge_strategy": "stream", "stream_limit": 5},
    )
    assert json.loads(_text(r))["status"] == "ok"
    assert fake_client.posts[-1] == {
        "merge_variables": {"log": ["x"]},
        "merge_strategy": "stream",
        "stream_limit": 5,
    }


async def test_status_and_dashboard(fake_client):
    s = _text(await server.mcp.call_tool("trmnl_status", {}))
    assert '"pushes_left_this_hour": 12' in s and "[1] WETTER 9°C" in s
    d = _text(await server.mcp.call_tool("trmnl_dashboard", {}))
    assert "[2] TRAINING" in d


async def test_quota_error_is_reported_not_raised(fake_client, monkeypatch):
    monkeypatch.setattr(server._client.pushlog, "limit", 0)
    r = await server.mcp.call_tool("trmnl_set_slot", {"slot": 1, "lines": ["x"]})
    assert "ERROR: Local quota exhausted" in _text(r)
    assert fake_client.posts == []


async def test_status_never_shows_the_plugin_uuid(tmp_path, monkeypatch):
    secret = "0f1e2d3c-4b5a-6978-8a9b-acbdcedfe0f1"
    cfg = Config(plugin_uuid=secret, state_dir=tmp_path, source="test")
    monkeypatch.setattr(server, "_client", TrmnlClient(cfg, http=FakeHttp()))
    s = _text(await server.mcp.call_tool("trmnl_status", {}))
    assert secret not in s and secret[4:] not in s
    assert "custom_plugins/0f1e…" in s
