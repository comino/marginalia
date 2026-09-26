"""Tests for the autopilot: TRMNL mirror text/rate limits and agent triggering."""

import asyncio
import sys

from remarkable_mcp.workflows import autopilot

ATT = {
    "attention": [
        {
            "kind": "review",
            "id": "duckdb",
            "title": "A Disposable DuckDB Workspace · v1",
            "status": "annotated",
        },
        {"kind": "form", "id": "q1", "title": "Question: Ship it?", "status": "annotated"},
        {"kind": "inbox", "id": "default", "title": "Agent Inbox", "status": "annotated"},
    ],
    "waiting": 2,
}
QUIET = {"attention": [], "waiting": 3}


def test_trmnl_lines_follow_slot_rules():
    lines = autopilot.trmnl_lines(ATT)
    assert lines[0] == "REMARKABLE: 3 neu"
    assert len(lines) <= 3 and all(len(ln) <= 45 for ln in lines)
    assert lines[1].startswith("Review: A Disposable DuckDB")
    assert autopilot.trmnl_lines(QUIET) == ["REMARKABLE: nichts Neues", "3 warten auf dich"]


def test_mirror_only_on_change_and_rate_limited(monkeypatch):
    pilot = autopilot.Autopilot({**autopilot.DEFAULTS, "trmnl_min_interval_seconds": 900})
    pushed = []
    monkeypatch.setattr(
        "remarkable_mcp.trmnl.tools.trmnl_set_slots", lambda slots: pushed.append(slots) or "{}"
    )
    monkeypatch.setattr("remarkable_mcp.trmnl.tools.configured", lambda: True)
    assert pilot.mirror(ATT) is not None
    assert pilot.mirror(ATT) is None  # unchanged text: no push
    assert pilot.mirror(QUIET) is None  # changed, but inside the rate window
    pilot.last_trmnl_push -= 1000
    assert pilot.mirror(QUIET) is not None
    assert [list(p) for p in pushed] == [["5"], ["5"]]


def test_mirror_disabled_without_slot():
    pilot = autopilot.Autopilot({**autopilot.DEFAULTS, "trmnl_slot": None})
    assert pilot.mirror(ATT) is None


def test_agent_runs_once_per_state(tmp_path, monkeypatch):
    monkeypatch.setenv("REMARKABLE_WORKFLOW_STATE", str(tmp_path))
    marker = tmp_path / "ran.txt"
    cfg = {
        **autopilot.DEFAULTS,
        "agent_command": [
            sys.executable,
            "-c",
            f"open({str(marker)!r}, 'a').write('x')",
            "{prompt}",
        ],
        "min_agent_interval_seconds": 0,
    }
    pilot = autopilot.Autopilot(cfg)
    assert asyncio.run(pilot.maybe_run_agent(ATT)) == 0
    assert asyncio.run(pilot.maybe_run_agent(ATT)) is None  # same state: not again
    assert asyncio.run(pilot.maybe_run_agent(QUIET)) is None  # nothing to do
    changed = {"attention": ATT["attention"][:1]}
    assert asyncio.run(pilot.maybe_run_agent(changed)) == 0
    assert marker.read_text() == "xx"
    assert "agent run" in (tmp_path / "autopilot.log").read_text()


def test_agent_off_by_default():
    pilot = autopilot.Autopilot(dict(autopilot.DEFAULTS))
    assert pilot.agent_argv() is None
    assert asyncio.run(pilot.maybe_run_agent(ATT)) is None


def test_prompt_is_substituted():
    pilot = autopilot.Autopilot(
        {**autopilot.DEFAULTS, "agent_command": ["claude", "-p", "{prompt}"]}
    )
    argv = pilot.agent_argv()
    assert argv[:2] == ["claude", "-p"] and "remarkable_whats_new" in argv[2]


def test_missing_agent_command_does_not_crash(tmp_path, monkeypatch):
    monkeypatch.setenv("REMARKABLE_WORKFLOW_STATE", str(tmp_path))
    cfg = {
        **autopilot.DEFAULTS,
        "agent_command": ["/nonexistent/claude", "{prompt}"],
        "min_agent_interval_seconds": 0,
    }
    pilot = autopilot.Autopilot(cfg)
    assert asyncio.run(pilot.maybe_run_agent(ATT)) == -2
    assert pilot.last_agent_signature is None  # not marked handled: retried later
    assert "could not start" in (tmp_path / "autopilot.log").read_text()


def test_hung_agent_is_killed_with_its_children(tmp_path, monkeypatch):
    import os

    monkeypatch.setenv("REMARKABLE_WORKFLOW_STATE", str(tmp_path))
    pidfile = tmp_path / "child.pid"
    script = (
        "import subprocess, sys, time;"
        f"p = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)']);"
        f"open({str(pidfile)!r}, 'w').write(str(p.pid));"
        "time.sleep(60)"
    )
    cfg = {
        **autopilot.DEFAULTS,
        "agent_command": [sys.executable, "-c", script],
        "agent_timeout_seconds": 1.5,
        "min_agent_interval_seconds": 0,
    }
    pilot = autopilot.Autopilot(cfg)
    assert asyncio.run(pilot.maybe_run_agent(ATT)) == -1
    child = int(pidfile.read_text())
    import time as _t

    for _ in range(50):
        try:
            os.kill(child, 0)
        except ProcessLookupError:
            break
        _t.sleep(0.1)
    else:
        raise AssertionError("grandchild survived the timeout")


def test_trmnl_state_survives_restart(tmp_path, monkeypatch):
    monkeypatch.setenv("REMARKABLE_WORKFLOW_STATE", str(tmp_path))
    pushed = []
    monkeypatch.setattr(
        "remarkable_mcp.trmnl.tools.trmnl_set_slots", lambda slots: pushed.append(slots) or "{}"
    )
    monkeypatch.setattr("remarkable_mcp.trmnl.tools.configured", lambda: True)
    first = autopilot.Autopilot(dict(autopilot.DEFAULTS))
    assert first.mirror(ATT) is not None
    restarted = autopilot.Autopilot(dict(autopilot.DEFAULTS))  # e.g. systemd restart
    assert restarted.mirror(ATT) is None  # same text already on the display
    assert restarted.mirror(QUIET) is None and restarted.mirror_due is not None  # scheduled
    assert len(pushed) == 1


def test_check_never_raises(monkeypatch, tmp_path):
    monkeypatch.setenv("REMARKABLE_WORKFLOW_STATE", str(tmp_path))
    pilot = autopilot.Autopilot(dict(autopilot.DEFAULTS))

    async def broken():
        raise RuntimeError("state unreadable")

    monkeypatch.setattr(pilot, "overview", broken)
    assert "_error" in asyncio.run(pilot.check())

    async def fine():
        return ATT

    def mirror_boom(ov):
        raise TimeoutError("TRMNL read timed out")

    monkeypatch.setattr(pilot, "overview", fine)
    monkeypatch.setattr(pilot, "mirror", mirror_boom)
    assert asyncio.run(pilot.check()) == ATT


def test_unit_file_is_sane():
    from pathlib import Path

    unit = (Path(__file__).parent / "contrib" / "remarkable-autopilot.service").read_text()
    assert "Environment=PATH=%h/.local/bin" in unit
    assert "StartLimitBurst" in unit and "Restart=on-failure" in unit
    assert "@main" not in unit  # pinned, not tracking a branch
