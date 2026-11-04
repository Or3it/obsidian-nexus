"""Tests for cascade/config_watcher.py — CascadeConfigWatcher and _ChangeHandler."""

import threading
import time
from unittest.mock import MagicMock

from orchestrator.cascade.config_watcher import CascadeConfigWatcher, _ChangeHandler

# ---------------------------------------------------------------------------
# _ChangeHandler — unit tests (no watchdog observer needed)
# ---------------------------------------------------------------------------


class TestChangeHandler:
    def test_calls_callback_for_target_file(self, tmp_path) -> None:
        target = tmp_path / "cascade.yaml"
        target.touch()
        calls: list[int] = []
        handler = _ChangeHandler(target, lambda: calls.append(1))
        event = MagicMock()
        event.src_path = str(target)
        handler.on_modified(event)
        assert len(calls) == 1

    def test_ignores_other_file_in_same_dir(self, tmp_path) -> None:
        target = tmp_path / "cascade.yaml"
        other = tmp_path / "other.yaml"
        calls: list[int] = []
        handler = _ChangeHandler(target, lambda: calls.append(1))
        event = MagicMock()
        event.src_path = str(other)
        handler.on_modified(event)
        assert len(calls) == 0

    def test_resolves_symlinks_for_comparison(self, tmp_path) -> None:
        target = tmp_path / "cascade.yaml"
        target.touch()
        link = tmp_path / "cascade_link.yaml"
        link.symlink_to(target)
        calls: list[int] = []
        handler = _ChangeHandler(target, lambda: calls.append(1))
        event = MagicMock()
        event.src_path = str(link)
        # The link resolves to the same file so the callback fires.
        handler.on_modified(event)
        assert len(calls) == 1

    def test_multiple_modifications_call_callback_each_time(self, tmp_path) -> None:
        target = tmp_path / "cascade.yaml"
        target.touch()
        calls: list[int] = []
        handler = _ChangeHandler(target, lambda: calls.append(1))
        event = MagicMock()
        event.src_path = str(target)
        handler.on_modified(event)
        handler.on_modified(event)
        assert len(calls) == 2


# ---------------------------------------------------------------------------
# CascadeConfigWatcher lifecycle
# ---------------------------------------------------------------------------


class TestWatcherLifecycle:
    def test_stop_before_start_is_safe(self, tmp_path) -> None:
        target = tmp_path / "cascade.yaml"
        target.touch()
        watcher = CascadeConfigWatcher(target, lambda: None)
        watcher.stop()  # must not raise

    def test_start_twice_is_idempotent(self, tmp_path) -> None:
        target = tmp_path / "cascade.yaml"
        target.touch()
        watcher = CascadeConfigWatcher(target, lambda: None)
        try:
            watcher.start()
            obs_first = watcher._observer
            watcher.start()
            assert watcher._observer is obs_first
        finally:
            watcher.stop()

    def test_stop_cancels_pending_debounce(self, tmp_path) -> None:
        target = tmp_path / "cascade.yaml"
        target.touch()
        calls: list[int] = []
        watcher = CascadeConfigWatcher(target, lambda: calls.append(1))
        # Plant a long-running timer directly.
        timer = threading.Timer(5.0, lambda: calls.append(1))
        watcher._debounce_timer = timer
        timer.start()
        watcher.stop()
        time.sleep(0.05)
        assert len(calls) == 0

    def test_observer_is_none_after_stop(self, tmp_path) -> None:
        target = tmp_path / "cascade.yaml"
        target.touch()
        watcher = CascadeConfigWatcher(target, lambda: None)
        watcher.start()
        watcher.stop()
        assert watcher._observer is None


# ---------------------------------------------------------------------------
# Debounce behaviour
# ---------------------------------------------------------------------------


class TestDebounce:
    def test_single_change_fires_callback_after_delay(self, tmp_path) -> None:
        target = tmp_path / "cascade.yaml"
        target.touch()
        calls: list[int] = []
        watcher = CascadeConfigWatcher(target, lambda: calls.append(1))
        watcher._debounced_change()
        time.sleep(0.65)  # debounce is 500 ms
        assert len(calls) == 1

    def test_rapid_changes_produce_one_callback(self, tmp_path) -> None:
        target = tmp_path / "cascade.yaml"
        target.touch()
        calls: list[int] = []
        watcher = CascadeConfigWatcher(target, lambda: calls.append(1))
        # Three rapid triggers — each resets the timer.
        watcher._debounced_change()
        watcher._debounced_change()
        watcher._debounced_change()
        time.sleep(0.65)
        assert len(calls) == 1
