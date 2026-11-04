"""In-memory process lifecycle for a project's run_command (Phase 3B minimal).

Process state (PID, output ring buffer) lives only in this process's memory —
it does not survive an orchestrator restart. This is a deliberate v1 tradeoff;
see OPEN_QUESTIONS.md ("process-state-not-persisted").
"""

from __future__ import annotations

import asyncio
import logging
import shlex
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

_OUTPUT_CAP = 200


@dataclass
class _RunningProcess:
    proc: asyncio.subprocess.Process
    output: deque[str] = field(default_factory=lambda: deque(maxlen=_OUTPUT_CAP))


class ProjectRunner:
    """Tracks at most one live subprocess per project slug."""

    def __init__(self, terminate_timeout: float = 5.0, kill_timeout: float = 2.0) -> None:
        self._running: dict[str, _RunningProcess] = {}
        self._terminate_timeout = terminate_timeout
        self._kill_timeout = kill_timeout

    async def start(self, slug: str, run_command: str, cwd: Path) -> dict[str, Any]:
        """Spawn ``run_command`` for *slug*, cwd=*cwd*. Idempotent if already running.

        Raises:
            ValueError: run_command is empty.
        """
        if not run_command or not run_command.strip():
            raise ValueError("no run_command configured for this project")

        existing = self._running.get(slug)
        if existing is not None and existing.proc.returncode is None:
            return self.status(slug)  # already running — idempotent, no double-spawn

        args = shlex.split(run_command)
        proc = await asyncio.create_subprocess_exec(
            *args,
            cwd=str(cwd),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
        running = _RunningProcess(proc=proc)
        self._running[slug] = running
        asyncio.ensure_future(self._pump(running))
        return self.status(slug)

    async def _pump(self, running: _RunningProcess) -> None:
        """Read combined stdout/stderr into the ring buffer until EOF, then reap."""
        assert running.proc.stdout is not None
        try:
            while True:
                line = await running.proc.stdout.readline()
                if not line:
                    break
                running.output.append(line.decode("utf-8", errors="replace").rstrip("\n"))
        except Exception as exc:  # noqa: BLE001
            log.warning("project runner output pump crashed: %s", exc)
        await running.proc.wait()

    async def stop(self, slug: str) -> dict[str, Any]:
        """Terminate the running process for *slug*. Idempotent if not running.

        SIGTERM first; escalates to SIGKILL if it hasn't exited within 5s.
        Waits for the exit to be reaped before returning, so the returned
        status accurately reflects reality (avoids the race where returncode
        is still None immediately after terminate() because reaping happens
        in the output-pump task).
        """
        running = self._running.get(slug)
        if running is None or running.proc.returncode is not None:
            return self.status(slug)  # not running — idempotent no-op
        running.proc.terminate()
        try:
            await asyncio.wait_for(running.proc.wait(), timeout=self._terminate_timeout)
        except TimeoutError:
            log.warning("project %s did not exit after SIGTERM; sending SIGKILL", slug)
            running.proc.kill()
            try:
                await asyncio.wait_for(running.proc.wait(), timeout=self._kill_timeout)
            except TimeoutError:
                log.warning("project %s did not exit even after SIGKILL", slug)
        return self.status(slug)

    def status(self, slug: str) -> dict[str, Any]:
        running = self._running.get(slug)
        if running is None:
            return {"running": False, "pid": None, "output": []}
        is_running = running.proc.returncode is None
        return {
            "running": is_running,
            "pid": running.proc.pid if is_running else None,
            "output": list(running.output),
        }
