"""Autopilot: react to the tablet without anyone opening a session.

Runs the live watcher. When ink changes and settles, it checks every tracked
workflow (the same check as ``remarkable_whats_new``) and starts a headless
agent (e.g. ``claude -p``) with the tablet_check_in prompt when something
needs attention. What the agent does then - reply on the tablet, post to a
TRMNL display, open an issue - is up to its prompt and tools; the autopilot
itself only knows the tablet.

Configuration: ``~/.config/remarkable-mcp/autopilot.json`` (all keys optional)::

    {
      "agent_command": ["claude", "-p", "{prompt}", "--allowedTools", "mcp__remarkable"],
      "prompt": "...",                 # default: the tablet_check_in prompt
      "debounce_seconds": 60,          # quiet time after the last change
      "min_agent_interval_seconds": 300,
      "agent_timeout_seconds": 900
    }

``agent_command`` is off by default: the agent then acts with your
permissions while you're away, so switch it on deliberately.

CLI::

    remarkable-autopilot            run forever
    remarkable-autopilot --once     one check now (prints what it would do)
    remarkable-autopilot --dry-run  run forever, but never start agents
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import List, Optional

from remarkable_mcp.workflows.state import state_root

logger = logging.getLogger("remarkable_autopilot")


def config_path() -> Path:
    return Path(
        os.environ.get("REMARKABLE_AUTOPILOT_CONFIG")
        or Path.home() / ".config" / "remarkable-mcp" / "autopilot.json"
    )


DEFAULTS = {
    "agent_command": None,
    "prompt": None,
    "debounce_seconds": 60,
    "min_agent_interval_seconds": 300,
    "agent_timeout_seconds": 900,
    "watch_min_refresh_seconds": 30,
}

# Keys of earlier versions (the TRMNL status mirror); ignored with a warning.
_RETIRED = ("trmnl_slot", "trmnl_min_interval_seconds")


def load_config() -> dict:
    cfg = dict(DEFAULTS)
    path = config_path()
    if path.is_file():
        cfg.update(json.loads(path.read_text()))
    for key in _RETIRED:
        if cfg.pop(key, None) is not None:
            logger.warning(
                "%s: %r is no longer used - the autopilot does not drive the TRMNL "
                "display; let the agent use the trmnl_* tools instead",
                path,
                key,
            )
    return cfg


def default_prompt() -> str:
    from remarkable_mcp.workflows.prompts import tablet_check_in_prompt

    return tablet_check_in_prompt()[0]["content"]


class Autopilot:
    def __init__(self, config: dict, dry_run: bool = False):
        self.cfg = config
        self.dry_run = dry_run
        self.last_agent_run = 0.0
        self.last_agent_signature: Optional[str] = None
        self.log_path = state_root() / "autopilot.log"
        self.overview_failures = 0

    # ------------------------------------------------------------ checks

    async def overview(self) -> dict:
        from remarkable_mcp.workflows.overview_tools import remarkable_whats_new

        return json.loads(await remarkable_whats_new())

    async def check(self) -> dict:
        """One pass: overview -> maybe agent. Never raises."""
        try:
            ov = await self.overview()
        except Exception as exc:  # corrupt state, network ... the loop must survive
            ov = {"_error": str(exc)}
        if "_error" in ov:
            self.overview_failures += 1
            logger.warning("overview failed: %s", ov["_error"])
            return ov
        self.overview_failures = 0
        try:
            await self.maybe_run_agent(ov)
        except Exception as exc:
            logger.warning("agent run failed: %s", exc)
        return ov

    # ------------------------------------------------------------ agent

    @staticmethod
    def signature(overview: dict) -> str:
        return json.dumps(
            sorted((a["kind"], a["id"], a["status"]) for a in overview.get("attention", []))
        )

    def agent_argv(self) -> Optional[List[str]]:
        cmd = self.cfg.get("agent_command")
        if not cmd:
            return None
        prompt = self.cfg.get("prompt") or default_prompt()
        return [part.replace("{prompt}", prompt) for part in cmd]

    async def maybe_run_agent(self, overview: dict) -> Optional[int]:
        argv = self.agent_argv()
        if argv is None or not overview.get("attention"):
            return None
        sig = self.signature(overview)
        if sig == self.last_agent_signature:
            return None  # already handled this exact state
        if time.time() - self.last_agent_run < self.cfg["min_agent_interval_seconds"]:
            return None
        self.last_agent_run = time.time()
        if self.dry_run:
            self.last_agent_signature = sig
            logger.info("[dry-run] would run agent: %s", argv[0])
            return 0
        logger.info("starting agent for %d item(s)", len(overview["attention"]))
        self.log_path.parent.mkdir(parents=True, exist_ok=True)
        with open(self.log_path, "a") as log:
            log.write(f"\n=== {time.strftime('%Y-%m-%d %H:%M:%S')} agent run: {sig}\n")
            log.flush()
            code = await asyncio.to_thread(_run_agent, argv, log, self.cfg["agent_timeout_seconds"])
        if code == 0:
            self.last_agent_signature = sig  # handled; a failed run is retried later
        logger.info("agent finished with %s", code)
        return code

    # ------------------------------------------------------------ loop

    async def run(self) -> None:
        from remarkable_mcp.workflows import live

        watcher = live.Watcher(min_refresh=self.cfg["watch_min_refresh_seconds"])
        queue = watcher.subscribe()
        task = asyncio.create_task(watcher.run())
        logger.info("autopilot running (dry_run=%s)", self.dry_run)
        await self.check()
        debounce = self.cfg["debounce_seconds"]
        try:
            while True:
                if task.done():  # the watcher must never be silently dead
                    exc = None if task.cancelled() else task.exception()
                    logger.warning("watcher stopped (%r); restarting it", exc)
                    task = asyncio.create_task(watcher.run())
                try:
                    await asyncio.wait_for(queue.get(), timeout=900.0)
                except asyncio.TimeoutError:
                    await self.check()  # periodic safety net
                    continue
                # Wait for the user to pause: reset the timer on every new change.
                while True:
                    try:
                        await asyncio.wait_for(queue.get(), timeout=debounce)
                    except asyncio.TimeoutError:
                        break
                await self.check()
        finally:
            task.cancel()
            await asyncio.wait({task}, timeout=5)  # let the socket close


def _run_agent(argv: List[str], log, timeout: float) -> int:
    """Run the agent in its own process group; on timeout kill the whole group
    (claude -p spawns MCP servers that would otherwise be orphaned)."""
    try:
        proc = subprocess.Popen(
            argv,
            stdout=log,
            stderr=subprocess.STDOUT,
            cwd=str(Path.home()),
            start_new_session=True,
        )
    except OSError as exc:  # command not found / not executable
        log.write(f"agent could not start: {exc}\n")
        return -2
    try:
        return proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        log.write("agent timed out; killing its process group\n")
        try:
            os.killpg(proc.pid, signal.SIGTERM)
            proc.wait(timeout=10)
        except (ProcessLookupError, subprocess.TimeoutExpired):
            pass
        try:
            # Grandchildren that ignore SIGTERM outlive the leader: always finish
            # the whole group off.
            os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        return -1


def main(argv: Optional[List[str]] = None) -> None:
    parser = argparse.ArgumentParser(
        prog="remarkable-autopilot", description=__doc__.split("\n")[0]
    )
    parser.add_argument("--once", action="store_true", help="check once and exit (dry run)")
    parser.add_argument(
        "--dry-run", action="store_true", help="never push to TRMNL or start agents"
    )
    args = parser.parse_args(argv)
    logging.basicConfig(
        level=logging.INFO, stream=sys.stderr, format="%(asctime)s %(name)s %(message)s"
    )
    cfg = load_config()
    if not (args.once or args.dry_run or cfg.get("agent_command")):
        # Watching the tablet costs sync-API requests shared with every client;
        # without an agent to start there is nothing to watch for.
        logger.info(
            "nothing to do: no agent_command in %s - the autopilot only starts agents",
            config_path(),
        )
        return
    pilot = Autopilot(cfg, dry_run=args.dry_run or args.once)
    if args.once:
        ov = asyncio.run(pilot.check())
        print(
            json.dumps(
                {
                    "overview": ov,
                    "agent": pilot.agent_argv() is not None,
                },
                indent=2,
                ensure_ascii=False,
            )
        )
        return
    try:
        asyncio.run(_serve(pilot))
    except KeyboardInterrupt:
        logger.info("autopilot stopped")


async def _serve(pilot: "Autopilot") -> None:
    """Run until SIGTERM (how systemd stops the service), then shut down cleanly."""
    main_task = asyncio.current_task()
    asyncio.get_running_loop().add_signal_handler(signal.SIGTERM, main_task.cancel)
    try:
        await pilot.run()
    except asyncio.CancelledError:
        logger.info("autopilot stopped")


if __name__ == "__main__":
    main()
