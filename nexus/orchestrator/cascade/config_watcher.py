"""File watcher for Config/cascade.yaml. Calls a reload callback on changes.

Editors often write config files in multiple steps (truncate then write, or
atomic rename). The 500 ms debounce prevents reload storms.
"""

import threading
from collections.abc import Callable
from pathlib import Path

from watchdog.events import FileSystemEvent, FileSystemEventHandler
from watchdog.observers import Observer
from watchdog.observers.api import BaseObserver


class CascadeConfigWatcher:
    def __init__(self, config_path: Path, on_change: Callable[[], None]) -> None:
        self.config_path = config_path.resolve()
        self.on_change = on_change
        self._observer: BaseObserver | None = None
        self._debounce_timer: threading.Timer | None = None

    def start(self) -> None:
        if self._observer:
            return
        handler = _ChangeHandler(self.config_path, self._debounced_change)
        self._observer = Observer()
        self._observer.schedule(handler, str(self.config_path.parent), recursive=False)
        self._observer.start()

    def stop(self) -> None:
        if self._debounce_timer:
            self._debounce_timer.cancel()
            self._debounce_timer = None
        if self._observer:
            self._observer.stop()
            self._observer.join(timeout=2)
            self._observer = None

    def _debounced_change(self) -> None:
        if self._debounce_timer:
            self._debounce_timer.cancel()
        self._debounce_timer = threading.Timer(0.5, self.on_change)
        self._debounce_timer.start()


class _ChangeHandler(FileSystemEventHandler):
    def __init__(self, target: Path, callback: Callable[[], None]) -> None:
        super().__init__()
        self.target = target
        self.callback = callback

    def _matches(self, path: str) -> bool:
        return Path(path).resolve() == self.target

    def on_modified(self, event: FileSystemEvent) -> None:
        if self._matches(str(event.src_path)):
            self.callback()

    def on_created(self, event: FileSystemEvent) -> None:
        # Atomic writes (e.g. write-then-rename) may trigger on_created instead
        # of on_modified for the target file.
        if self._matches(str(event.src_path)):
            self.callback()

    def on_moved(self, event: FileSystemEvent) -> None:
        # Some editors write to a temp file and rename it to the target path.
        if hasattr(event, "dest_path") and self._matches(str(event.dest_path)):
            self.callback()
