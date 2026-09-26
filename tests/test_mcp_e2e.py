"""End-to-end through the real MCP layer: argument parsing/coercion as clients send it,
tool registration, and docs/prompts that only name tools which exist."""

import asyncio
import json
import re
from pathlib import Path

import pytest

from remarkable_mcp.server import mcp
from test_forms import _tick
from test_workflows import (  # noqa: F401
    DRAFT,
    FINELINER,
    _fake_path,
    _page_w,
    _phrase_rects,
    _strike,
    cloud,
)

ROOT = Path(__file__).parent.parent
# Names that look like tools but are not registered in cloud mode / are config keys.
NOT_TOOLS = {
    "remarkable_mcp",  # the Python package
    "remarkable_author",  # SSH transport only
    "trmnl_slot",  # autopilot config keys
    "trmnl_min_interval_seconds",
}


def _call(name, args):
    result = asyncio.run(mcp.call_tool(name, args))
    blocks = result.content if hasattr(result, "content") else result
    text = next(b.text for b in blocks if getattr(b, "type", "") == "text")
    return json.loads(text), blocks


def _tool_names():
    return {t.name for t in asyncio.run(mcp.list_tools())}


def _trmnl_tool_names():
    """TRMNL tools, whether or not a display is configured where the tests run
    (the server registers them at import time only when one is)."""
    from mcp.server.mcpserver import MCPServer

    from remarkable_mcp.trmnl import tools as trmnl_tools

    scratch = MCPServer(name="trmnl-names")
    trmnl_tools.register(scratch)
    return {t.name for t in asyncio.run(scratch.list_tools())}


def test_every_tool_named_in_docs_and_prompts_exists():
    names = _tool_names() | _trmnl_tool_names()
    sources = {
        "README.md": (ROOT / "README.md").read_text(),
        "docs/workflows.md": (ROOT / "docs" / "workflows.md").read_text(),
        "SKILL.md": (ROOT / "SKILL.md").read_text(),
        "prompts.py": (ROOT / "remarkable_mcp" / "workflows" / "prompts.py").read_text(),
    }
    missing = {}
    for src, text in sources.items():
        for name in set(re.findall(r"\b(remarkable_[a-z_]+|trmnl_[a-z_]+)\b", text)):
            base = name.rstrip("_")
            if base.endswith("_") or base in NOT_TOOLS:
                continue
            # "remarkable_reading_*" style wildcards in tables
            if base in names or any(n.startswith(base + "_") for n in names):
                continue
            missing.setdefault(src, []).append(name)
    assert not missing, missing


def test_every_workflow_tool_is_documented():
    docs = (ROOT / "docs" / "workflows.md").read_text() + (ROOT / "README.md").read_text()
    core = (ROOT / "docs" / "core.md").read_text()
    undocumented = [
        n for n in _tool_names() if n not in docs and n not in core and not n.startswith("trmnl_")
    ]
    assert not undocumented, undocumented


def test_every_tool_has_usecase_and_described_parameters():
    """Workflow tools document every parameter, so small models know what to pass."""
    from remarkable_mcp.workflows import overview as overview_tools
    from remarkable_mcp.workflows import tools
    from remarkable_mcp.workflows.forms import tools as form_tools
    from remarkable_mcp.workflows.inbox import tools as inbox_tools
    from remarkable_mcp.workflows.live import tools as live_tools
    from remarkable_mcp.workflows.reading import tools as reading_tools
    from remarkable_mcp.workflows.review import code_tools as code_review_tools
    from remarkable_mcp.workflows.review import latex_tools
    from remarkable_mcp.workflows.structure import sketch_tools
    from remarkable_mcp.workflows.structure import tools as structure_tools

    modules = [
        code_review_tools,
        form_tools,
        inbox_tools,
        latex_tools,
        live_tools,
        overview_tools,
        reading_tools,
        sketch_tools,
        structure_tools,
        tools,
    ]
    workflow_fns = {
        name
        for m in modules
        for name in dir(m)
        if name.startswith("remarkable_") and callable(getattr(m, name))
    }
    problems = []
    for tool in asyncio.run(mcp.list_tools()):
        desc = tool.description or ""
        if "<usecase>" not in desc:
            problems.append((tool.name, "<usecase>"))
        if tool.name not in workflow_fns:
            continue
        for param in (tool.input_schema or {}).get("properties", {}):
            if not any(f in desc for f in (f"- {param}", f"`{param}`", f"{param}:", f"- {param} ")):
                problems.append((tool.name, param))
    assert not problems, problems


def test_ask_and_read_through_mcp(cloud):  # noqa: F811
    sent, _ = _call("remarkable_ask", {"question": "Ship it?", "options": ["Yes", "No", "Later"]})
    from remarkable_mcp.workflows.forms import tools as form_tools

    record = form_tools._forms().get(sent["form"])
    later = next(a for a in record["manifest"]["areas"] if a["option"] == "Later")
    cloud.annotate(record["doc_id"], {0: [(_tick(tuple(later["rect"])), FINELINER, 446.0)]})
    got, _ = _call("remarkable_form_read", {"form": sent["form"]})
    assert got["values"]["answer"] == "Later" and got["answered"] is True


def test_review_round_trip_through_mcp(cloud):  # noqa: F811
    sent, _ = _call("remarkable_review_send", {"markdown": DRAFT})
    doc = next(d for d in cloud.docs.values() if d.VissibleName.endswith("· v1"))
    pdf = cloud.zips[doc.id]
    pno, rects = _phrase_rects(pdf, "permission to fetch data")
    cloud.annotate(doc.id, {pno: [(_strike(rects), FINELINER, _page_w(pdf))]})
    got, _ = _call("remarkable_review_collect", {"review": sent["review"]})
    [req] = got["requests"]
    assert req["intent"] == "delete" and req["src_line"] == 6
    listed, _ = _call("remarkable_review_list", {})
    assert listed["reviews"][0]["status"] == "waiting"  # collected


def test_list_params_are_coerced(cloud):  # noqa: F811
    """Clients send JSON arrays; regions/pages/options must round-trip through validation."""
    form, _ = _call(
        "remarkable_form_send",
        {"title": "T", "fields": [{"id": "a", "type": "scale", "label": "A", "min": 1, "max": 3}]},
    )
    assert "form" in form
    triage, _ = _call(
        "remarkable_triage_send",
        {"title": "T", "items": [{"id": "x", "title": "X"}], "options": ["Go", "Stop"]},
    )
    assert "form" in triage


@pytest.mark.parametrize(
    "name,args,error",
    [
        ("remarkable_review_collect", {"review": "nope"}, "review_not_found"),
        ("remarkable_form_read", {"form": "nope"}, "form_not_found"),
        ("remarkable_inbox", {"name": "nope"}, "inbox_not_set_up"),
        ("remarkable_code_review_collect", {"review": "nope"}, "review_not_found"),
        ("remarkable_latex_review_collect", {"review": "nope"}, "review_not_found"),
        ("remarkable_review_send", {}, "invalid_arguments"),
        ("remarkable_form_send", {"title": "x", "fields": []}, "invalid_form"),
    ],
)
def test_errors_are_structured(cloud, name, args, error):  # noqa: F811
    got, _ = _call(name, args)
    assert got["_error"]["type"] == error
    assert got["_error"]["suggestion"]
