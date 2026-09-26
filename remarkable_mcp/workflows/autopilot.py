"""Autopilot: react to the tablet without anyone opening a session.

Runs the live watcher. When ink changes and settles, it checks every tracked
workflow (the same check as ``remarkable_whats_new``) and

- mirrors a short status into one TRMNL slot (rate-limited, only on change),
- optionally starts a headless agent (e.g. ``claude -p``) with the
  tablet_check_in prompt when something needs attention.

Configuration: ``~/.config/remarkable-mcp/autopilot.json`` (all keys optional)::

    {
      "agent_command": ["claude", "-p", "{prompt}", "--allowedTools", "mcp__remarkable"],
      "prompt": "...",                 # default: the tablet_check_in prompt
      "debounce_seconds": 60,          # quiet time after the last change
      "min_agent_interval_seconds": 300,
      "agent_timeout_seconds": 900,
      "trmnl_slot": 5,                 # null disables the mirror
      "trmnl_min_interval_seconds": 900
    }

``agent_command`` is off by default: the agent then acts with your
permissions while you're away, so switch it on deliberately.

CLI::

    remarkable-autopilot            run forever
    remarkable-autopilot --once     one check now (prints what it would do)
    remarkable-autopilot --dry-run  run forever, but never push or start agents
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

from remarkable_mcp.workflows.state import Store, state_root

logger = logging.getLogger("remarkable_autopilot")

CONFIG_PATH = Path(
    os.environ.get("REMARKABLE_AUTOPILOT_CONFIG")
    or Path.home() / ".config" / "remarkable-mcp" / "autopilot.json"
)

DEFAULTS = {
    "agent_command": None,
    "prompt": None,
    "debounce_seconds": 60,
    "min_agent_interval_seconds": 300,
    "agent_timeout_seconds": 900,
    "trmnl_slot": 5,
    "trmnl_min_interval_seconds": 900,
    "watch_min_refresh_seconds": 30,
}

_KIND_DE = {
    "review": "Review",
    "form": "Formular",
    "article": "Artikel",
    "inbox": "Inbox",
}
_STATUS_DE = {"annotated": "neue Tinte", "done": "fertig", "collected": "gelesen"}


def load_config() -> dict:
    cfg = dict(DEFAULTS)
    if CONFIG_PATH.is_file():
        cfg.update(json.loads(CONFIG_PATH.read_text()))
    return cfg


def default_prompt() -> str:
    from remarkable_mcp.workflows.prompts import tablet_check_in_prompt

    return tablet_check_in_prompt()[0]["content"]


def trmnl_lines(overview: dict) -> List[str]:
    """At most 3 lines of 45 chars (the TRMNL slot rules), German like the rest."""
    att = overview.get("attention", [])
    waiting = overview.get("waiting", 0)
    if not att:
        return ["REMARKABLE: nichts Neues", f"{waiting} warten auf dich" if waiting else ""]
    lines = [f"REMARKABLE: {len(att)} neu"]
    for item in att[:2]:
        kind = _KIND_DE.get(item["kind"], item["kind"])
        title = (item.get("title") or item["id"])[:28]
        lines.append(f"{kind}: {title}")
    return [ln[:45] for ln in lines if ln]


class Autopilot:
    def __init__(self, config: dict, dry_run: bool = False):
        self.cfg = config
        self.dry_run = dry_run
        self.last_agent_run = 0.0
        self.last_agent_signature: Optional[str] = None
        self.log_path = state_root() / "autopilot.log"
        # What is on the display survives restarts: a restart must not spend
        # one of the display's 12 pushes/hour re-sending the same text.
        self._store = Store("autopilot")
        saved = self._store.get("trmnl") or {}
        self.last_trmnl_push = float(saved.get("pushed_at", 0.0))
        self.last_trmnl_text: Optional[str] = saved.get("text")
        self.mirror_due: Optional[float] = None  # changed text waiting for the rate window

    # ------------------------------------------------------------ checks

    async def overview(self) -> dict:
        from remarkable_mcp.workflows.overview_tools import remarkable_whats_new

        return json.loads(await remarkable_whats_new())

    async def check(self) -> dict:
        """One pass: overview -> TRMNL mirror -> maybe agent. Never raises."""
        try:
            ov = await self.overview()
        except Exception as exc:  # corrupt state, network ... the loop must survive
            logger.warning("overview failed: %s", exc)
            return {"_error": str(exc)}
        if "_error" in ov:
            logger.warning("overview failed: %s", ov["_error"])
            return ov
        try:
            self.mirror(ov)
        except Exception as exc:
            logger.warning("TRMNL mirror failed: %s", exc)
        try:
            await self.maybe_run_agent(ov)
        except Exception as exc:
            logger.warning("agent run failed: %s", exc)
        return ov

    # ------------------------------------------------------------ TRMNL

    def mirror(self, overview: dict) -> Optional[str]:
        slot = self.cfg.get("trmnl_slot")
        if slot is None:
            return None
        text = "\n".join(trmnl_lines(overview))
        if text == self.last_trmnl_text:
            self.mirror_due = None
            return None
        wait = self.cfg["trmnl_min_interval_seconds"] - (time.time() - self.last_trmnl_push)
        if wait > 0:
            self.mirror_due = time.time() + wait  # re-check when the window opens
            return None
        self.mirror_due = None
        if self.dry_run:
            logger.info("[dry-run] TRMNL slot %s <- %r", slot, text)
            self.last_trmnl_text = text
            return text
        from remarkable_mcp.trmnl import tools as trmnl

        if not trmnl.configured():
            return None
        result = trmnl.trmnl_set_slots({str(slot): text.split("\n")})
        if result.startswith("ERROR"):
            logger.warning("TRMNL push failed: %s", result)
            return None
        self.last_trmnl_text = text
        self.last_trmnl_push = time.time()
        self._store.put("trmnl", {"text": text, "pushed_at": self.last_trmnl_push})
        logger.info("TRMNL slot %s updated", slot)
        return text

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
                idle = 900.0
                if self.mirror_due is not None:
                    idle = max(1.0, min(idle, self.mirror_due - time.time()))
                try:
                    await asyncio.wait_for(queue.get(), timeout=idle)
                except asyncio.TimeoutError:
                    await self.check()  # periodic safety net / pending TRMNL update
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
            try:
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
    pilot = Autopilot(load_config(), dry_run=args.dry_run or args.once)
    if args.once:
        ov = asyncio.run(pilot.check())
        print(
            json.dumps(
                {
                    "overview": ov,
                    "trmnl": trmnl_lines(ov) if "_error" not in ov else None,
                    "agent": pilot.agent_argv() is not None,
                },
                indent=2,
                ensure_ascii=False,
            )
        )
        return
    asyncio.run(pilot.run())


if __name__ == "__main__":
    main()
