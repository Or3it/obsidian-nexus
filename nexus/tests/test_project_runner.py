"""Tests for orchestrator.projects.runner.ProjectRunner — subprocess is mocked."""

from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from orchestrator.projects.runner import ProjectRunner


def _make_fake_proc(lines: list[bytes], pid: int = 4242) -> MagicMock:
    """Build a fake asyncio.subprocess.Process: readline() pops lines, then EOF."""
    proc = MagicMock()
    proc.pid = pid
    proc.returncode = None

    remaining = list(lines) + [b""]
    idx = [0]

    async def readline() -> bytes:
        i = idx[0]
        idx[0] += 1
        return remaining[i] if i < len(remaining) else b""

    proc.stdout = MagicMock()
    proc.stdout.readline = readline

    async def wait() -> int:
        proc.returncode = 0
        return 0

    proc.wait = wait
    proc.terminate = MagicMock()
    return proc


async def _settle() -> None:
    """Give the fire-and-forget output pump task a chance to run to completion."""
    for _ in range(20):
        await asyncio.sleep(0)


class TestStart:
    async def test_spawns_with_correct_args_and_cwd(self, tmp_path: Path) -> None:
        runner = ProjectRunner()
        fake = _make_fake_proc([])
        with patch(
            "asyncio.create_subprocess_exec", new=AsyncMock(return_value=fake)
        ) as spawn:
            await runner.start("proj", "python app.py --port 5000", tmp_path)

        spawn.assert_called_once()
        args, kwargs = spawn.call_args
        assert args == ("python", "app.py", "--port", "5000")
        assert kwargs["cwd"] == str(tmp_path)

    async def test_raises_value_error_when_no_run_command(self, tmp_path: Path) -> None:
        runner = ProjectRunner()
        with pytest.raises(ValueError, match="no run_command"):
            await runner.start("proj", "", tmp_path)

    async def test_raises_value_error_when_run_command_none_like(
        self, tmp_path: Path
    ) -> None:
        runner = ProjectRunner()
        with pytest.raises(ValueError, match="no run_command"):
            await runner.start("proj", "   ", tmp_path)

    async def test_returns_running_status(self, tmp_path: Path) -> None:
        runner = ProjectRunner()
        fake = _make_fake_proc([], pid=777)
        with patch("asyncio.create_subprocess_exec", new=AsyncMock(return_value=fake)):
            result = await runner.start("proj", "python app.py", tmp_path)
        assert result == {"running": True, "pid": 777, "output": []}

    async def test_idempotent_does_not_double_spawn(self, tmp_path: Path) -> None:
        runner = ProjectRunner()
        fake = _make_fake_proc([])  # readline immediately EOFs, but returncode
        fake.returncode = None      # stays None until _pump awaits proc.wait()
        with patch(
            "asyncio.create_subprocess_exec", new=AsyncMock(return_value=fake)
        ) as spawn:
            await runner.start("proj", "python app.py", tmp_path)
            # Second call before the pump task has settled — still "running"
            second = await runner.start("proj", "python app.py", tmp_path)

        spawn.assert_called_once()
        assert second["running"] is True

    async def test_restarts_after_process_exits(self, tmp_path: Path) -> None:
        runner = ProjectRunner()
        first = _make_fake_proc([])
        second = _make_fake_proc([], pid=999)
        with patch(
            "asyncio.create_subprocess_exec",
            new=AsyncMock(side_effect=[first, second]),
        ) as spawn:
            await runner.start("proj", "python app.py", tmp_path)
            await _settle()  # let the pump reach EOF and reap `first`
            assert runner.status("proj")["running"] is False

            result = await runner.start("proj", "python app.py", tmp_path)

        assert spawn.call_count == 2
        assert result == {"running": True, "pid": 999, "output": []}

    async def test_different_slugs_track_independently(self, tmp_path: Path) -> None:
        runner = ProjectRunner()
        proc_a = _make_fake_proc([], pid=1)
        proc_b = _make_fake_proc([], pid=2)
        with patch(
            "asyncio.create_subprocess_exec",
            new=AsyncMock(side_effect=[proc_a, proc_b]),
        ):
            await runner.start("a", "run-a", tmp_path)
            await runner.start("b", "run-b", tmp_path)

        assert runner.status("a")["pid"] == 1
        assert runner.status("b")["pid"] == 2


class TestOutputPump:
    async def test_output_lines_captured_in_order(self, tmp_path: Path) -> None:
        runner = ProjectRunner()
        fake = _make_fake_proc([b"hello\n", b"world\n"])
        with patch("asyncio.create_subprocess_exec", new=AsyncMock(return_value=fake)):
            await runner.start("proj", "python app.py", tmp_path)
            await _settle()

        assert runner.status("proj")["output"] == ["hello", "world"]

    async def test_ring_buffer_caps_at_200_lines(self, tmp_path: Path) -> None:
        runner = ProjectRunner()
        lines = [f"line-{i}\n".encode() for i in range(250)]
        fake = _make_fake_proc(lines)
        with patch("asyncio.create_subprocess_exec", new=AsyncMock(return_value=fake)):
            await runner.start("proj", "python app.py", tmp_path)
            await _settle()

        output = runner.status("proj")["output"]
        assert len(output) == 200
        # Oldest 50 lines evicted; buffer holds the most recent 200
        assert output[0] == "line-50"
        assert output[-1] == "line-249"


class TestStop:
    async def test_terminates_running_process(self, tmp_path: Path) -> None:
        runner = ProjectRunner()
        fake = _make_fake_proc([])
        fake.returncode = None
        with patch("asyncio.create_subprocess_exec", new=AsyncMock(return_value=fake)):
            await runner.start("proj", "python app.py", tmp_path)
            await runner.stop("proj")

        fake.terminate.assert_called_once()

    async def test_stop_idempotent_when_never_started(self, tmp_path: Path) -> None:
        runner = ProjectRunner()
        result = await runner.stop("never-started")
        assert result == {"running": False, "pid": None, "output": []}

    async def test_stop_idempotent_when_already_exited(self, tmp_path: Path) -> None:
        runner = ProjectRunner()
        fake = _make_fake_proc([])
        with patch("asyncio.create_subprocess_exec", new=AsyncMock(return_value=fake)):
            await runner.start("proj", "python app.py", tmp_path)
            await _settle()  # process reaches EOF, returncode set

        result = await runner.stop("proj")
        fake.terminate.assert_not_called()
        assert result["running"] is False

    async def test_stop_waits_for_exit_before_returning(self, tmp_path: Path) -> None:
        """The returned status must be accurate — not the transient race where
        returncode is still None right after terminate() is called."""
        runner = ProjectRunner()
        fake = _make_fake_proc([])
        with patch("asyncio.create_subprocess_exec", new=AsyncMock(return_value=fake)):
            await runner.start("proj", "python app.py", tmp_path)
            result = await runner.stop("proj")

        assert result["running"] is False
        assert result["pid"] is None

    async def test_stop_escalates_to_sigkill_after_terminate_timeout(
        self, tmp_path: Path
    ) -> None:
        """If the process ignores SIGTERM, stop() sends SIGKILL after the grace period.

        All concurrent proc.wait() callers (the output pump + stop() itself)
        share one exit signal, matching real asyncio subprocess semantics —
        terminate() is a no-op (simulating an unresponsive process); kill()
        is what actually lets every waiter resolve.
        """
        runner = ProjectRunner(terminate_timeout=0.05, kill_timeout=0.05)
        fake = _make_fake_proc([])
        fake.returncode = None
        exited = asyncio.Event()

        async def wait() -> int:
            await exited.wait()
            fake.returncode = 0
            return 0

        fake.wait = wait
        fake.terminate = MagicMock()  # no-op: simulates ignoring SIGTERM
        fake.kill = MagicMock(side_effect=exited.set)

        with patch("asyncio.create_subprocess_exec", new=AsyncMock(return_value=fake)):
            await runner.start("proj", "python app.py", tmp_path)
            result = await runner.stop("proj")

        fake.terminate.assert_called_once()
        fake.kill.assert_called_once()
        assert result["running"] is False
        assert result["running"] is False


class TestStatus:
    def test_unknown_slug_reports_not_running(self) -> None:
        runner = ProjectRunner()
        assert runner.status("nope") == {"running": False, "pid": None, "output": []}
