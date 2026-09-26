"""Tests for the autopilot: agent triggering, config, lifecycle."""

import asyncio
import sys

import pytest

from remarkable_mcp.workflows.live import autopilot


@pytest.fixture(autouse=True)
def isolated_state(tmp_path, monkeypatch):
    """Never read or write the real ~/.local/state from tests."""
    monkeypatch.setenv("REMARKABLE_WORKFLOW_STATE", str(tmp_path))


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


def test_autopilot_does_not_touch_the_trmnl_display():
    """TRMNL is a separate tool set for agents; the autopilot only knows the tablet."""
    import ast
    from pathlib import Path

    src = Path(autopilot.__file__).read_text()
    imported = {
        n.module if isinstance(n, ast.ImportFrom) else a.name
        for n in ast.walk(ast.parse(src))
        if isinstance(n, (ast.Import, ast.ImportFrom))
        for a in n.names
    }
    assert not any("trmnl" in (m or "") for m in imported)


def test_retired_trmnl_keys_are_ignored_with_a_warning(tmp_path, monkeypatch, caplog):
    cfg = tmp_path / "autopilot.json"
    cfg.write_text('{"trmnl_slot": 5, "trmnl_min_interval_seconds": 900, "debounce_seconds": 5}')
    monkeypatch.setenv("REMARKABLE_AUTOPILOT_CONFIG", str(cfg))
    with caplog.at_level("WARNING", logger="remarkable_autopilot"):
        loaded = autopilot.load_config()
    assert "trmnl_slot" not in loaded and "trmnl_min_interval_seconds" not in loaded
    assert loaded["debounce_seconds"] == 5
    assert "trmnl_slot" in caplog.text


def test_without_an_agent_the_daemon_exits(tmp_path, monkeypatch, caplog):
    monkeypatch.setenv("REMARKABLE_AUTOPILOT_CONFIG", str(tmp_path / "none.json"))

    def must_not_run(self):
        raise AssertionError("the watcher must not start without an agent")

    monkeypatch.setattr(autopilot.Autopilot, "run", must_not_run)
    with caplog.at_level("INFO", logger="remarkable_autopilot"):
        autopilot.main([])
    assert "nothing to do" in caplog.text


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


def test_check_never_raises(monkeypatch, tmp_path):
    monkeypatch.setenv("REMARKABLE_WORKFLOW_STATE", str(tmp_path))
    pilot = autopilot.Autopilot(dict(autopilot.DEFAULTS))

    async def broken():
        raise RuntimeError("state unreadable")

    monkeypatch.setattr(pilot, "overview", broken)
    assert "_error" in asyncio.run(pilot.check())

    async def fine():
        return ATT

    async def agent_boom(ov):
        raise TimeoutError("agent start timed out")

    monkeypatch.setattr(pilot, "overview", fine)
    monkeypatch.setattr(pilot, "maybe_run_agent", agent_boom)
    assert asyncio.run(pilot.check()) == ATT


def test_unit_file_is_sane():
    from pathlib import Path

    unit = (Path(__file__).parent.parent / "contrib" / "remarkable-autopilot.service").read_text()
    assert "Environment=PATH=%h/.local/bin" in unit
    assert "StartLimitBurst" in unit and "Restart=on-failure" in unit
    assert "@main" not in unit  # pinned, not tracking a branch


def test_sigterm_stops_the_daemon_cleanly(tmp_path):
    """systemd stops the service with SIGTERM: exit 0 and say so, not a crash."""
    import os
    import signal
    import subprocess
    import time

    # The real run loop, with a watcher whose shutdown needs an await (like
    # closing the notification socket) and a check that needs no network.
    script = (
        "import asyncio\n"
        "from remarkable_mcp.workflows.live import autopilot as a, watcher as live\n"
        "class FakeWatcher:\n"
        "    def __init__(self, **kw): self.q = asyncio.Queue()\n"
        "    def subscribe(self): return self.q\n"
        "    async def run(self):\n"
        "        try:\n"
        "            await asyncio.sleep(3600)\n"
        "        finally:\n"
        "            await asyncio.sleep(0.2)\n"
        "            print('watcher closed', flush=True)\n"
        "live.Watcher = FakeWatcher\n"
        "async def check(self):\n"
        "    print('ready', flush=True)\n"
        "a.Autopilot.check = check\n"
        "a.main([])\n"
    )
    cfg = tmp_path / "autopilot.json"
    cfg.write_text('{"agent_command": ["true"]}')  # without an agent it would just exit
    env = dict(os.environ, REMARKABLE_AUTOPILOT_CONFIG=str(cfg))
    proc = subprocess.Popen(
        [sys.executable, "-c", script],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=env,
    )
    try:
        assert proc.stdout.readline().strip() == "ready"
        time.sleep(0.2)
        proc.send_signal(signal.SIGTERM)
        out, err = proc.communicate(timeout=20)
    finally:
        proc.kill()
    assert proc.returncode == 0, err
    assert "autopilot stopped" in err
    assert "Traceback" not in err and "destroyed but it is pending" not in err
    assert "watcher closed" in out
