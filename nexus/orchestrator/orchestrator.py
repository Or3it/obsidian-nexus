"""Main orchestrator: wires together config, event log, projection, and cascade engine."""
import logging
import subprocess
from collections.abc import AsyncGenerator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import uuid_utils as uuid
import yaml

from .cascade.engine import CascadeEngine
from .cascade.events import (
    ClassificationEvent,
    ErrorEvent,
    HandoverEvent,
    MetaEvent,
    TokenEvent,
    ToolCallEvent,
    ToolResultEvent,
)
from .config import load_projects_config
from .heuristics.save_worthiness import is_worth_saving
from .projects.discovery import (
    _build_file_tree,
    _find_readme,
    _get_git_info,
    build_candidate,
    discover,
    slugify,
)
from .projects.runner import ProjectRunner
from .projects.store import (
    delete_project_note,
    read_project_note,
    read_project_notes,
    update_run_command,
    write_project_note,
)
from .projects.summarizer import summarize
from .prompts import build_project_context, compose_request_messages
from .types import NexusMessage, NexusRequest, ProjectsConfig, StyleProfile, TurnMode
from .vault.event_log import EventLog
from .vault.projection import kebab_title, project_thread, thread_filename

log = logging.getLogger(__name__)

_SENTINEL_TEXT = (
    "# Do Not Edit\n\n"
    "This folder is managed by the Obsidian Nexus orchestrator. "
    "Do not modify files here directly — changes will be overwritten.\n"
)


class Orchestrator:
    """Wires CascadeEngine, EventLog, and projection into a per-turn streaming API."""

    def __init__(self, vault_root: Path) -> None:
        self.vault_root = vault_root
        config_path = vault_root / "Orchestrator" / "Config" / "cascade.yaml"
        self.cascade = CascadeEngine(vault_root=vault_root, config_path=config_path)
        self._projects_config: ProjectsConfig = load_projects_config(config_path)
        self._runner = ProjectRunner()
        self._ensure_vault_layout()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def new_thread_id(self) -> str:
        return str(uuid.uuid7())

    async def send_turn_stream(
        self,
        thread_id: str,
        user_message: str,
        style_profile: StyleProfile | None = None,
        mode: TurnMode = "auto",
        include_projects: bool = False,
    ) -> AsyncGenerator[dict[str, Any], None]:
        """Append user turn, stream cascade events as dicts, append assistant turn + project.

        Yields plain dicts the server formats as SSE:
          {"type": "meta", "mode": str, "requested_mode": str, "model": str}
          {"type": "token", "text": str, "is_final": bool}
          {"type": "handover", "from_rung": int, "to_rung": int, ...}
          {"type": "error", "message": str, "terminal": bool}
        """
        log_obj = EventLog(self._log_path(thread_id))
        history = self._load_history(log_obj)
        turn_number = self._next_turn_number(history)
        # turn_user is appended at the end, alongside turn_assistant, once the
        # save-worthiness filter has seen the whole turn (P3B: skip low-value
        # turns). This is persistence-only — token streaming below happens
        # regardless, so the frontend's in-session history is unaffected.

        thread_yaml = self._load_thread_yaml(thread_id) or {}
        style = style_profile or StyleProfile()
        project_context: str | None = None
        if include_projects:
            project_context = build_project_context(read_project_notes(self.vault_root))
        messages = compose_request_messages(
            history, user_message, style, project_context=project_context
        )
        request = NexusRequest(
            thread_id=thread_id,
            messages=messages,
            style_profile=style,
            mode=mode,
        )

        # Resolve which rung will answer — initialises active_model before any handover.
        initial_rung = self.cascade.select_rung(thread_yaml)
        active_model = self.cascade._active_model_for_rung(initial_rung)
        live_history_filename = self._derive_live_filename(
            thread_id, log_obj, pending_user_message=user_message
        )
        started = datetime.now(UTC)

        chunks: list[str] = []
        async for event in self.cascade.complete_stream(
            request=request,
            thread_yaml=thread_yaml,
            live_history_filename=live_history_filename,
        ):
            if isinstance(event, ClassificationEvent):
                log_obj.append(
                    "turn_classified",
                    {
                        "task_class": event.task_class,
                        "confidence": event.confidence,
                        "reason": event.reason,
                        "backend": event.backend,
                        "mode": event.mode,
                    },
                )
            elif isinstance(event, MetaEvent):
                active_model = event.model
                yield {
                    "type": "meta",
                    "mode": event.mode,
                    "requested_mode": event.requested_mode,
                    "model": event.model,
                }
            elif isinstance(event, TokenEvent):
                if not event.token.is_final:
                    chunks.append(event.token.text)
                yield {"type": "token", "text": event.token.text, "is_final": event.token.is_final}
            elif isinstance(event, HandoverEvent):
                log_obj.append(
                    "handover",
                    {
                        "handover_id": event.handover_path.stem,
                        "from_rung": event.from_rung,
                        "to_rung": event.to_rung,
                        "from_model": event.from_model,
                        "to_model": event.to_model,
                        "trigger": event.trigger,
                        "reason": event.reason,
                    },
                )
                active_model = event.to_model
                yield {
                    "type": "handover",
                    "from_rung": event.from_rung,
                    "to_rung": event.to_rung,
                    "from_model": event.from_model,
                    "to_model": event.to_model,
                    "trigger": event.trigger,
                    "reason": event.reason,
                }
            elif isinstance(event, ToolCallEvent):
                yield {"type": "tool_call", "name": event.name, "input": event.input}
            elif isinstance(event, ToolResultEvent):
                yield {"type": "tool_result", "name": event.name, "content": event.content}
            elif isinstance(event, ErrorEvent):
                yield {"type": "error", "message": event.message, "terminal": event.terminal}
                if event.terminal:
                    return  # abort — no turn_user/turn_assistant written

        content = "".join(chunks)
        duration_ms = int((datetime.now(UTC) - started).total_seconds() * 1000)

        if is_worth_saving(user_message, content):
            log_obj.append(
                "turn_user",
                {"content": user_message, "turn_number": turn_number, "thread_id": thread_id},
            )
            log_obj.append(
                "turn_assistant",
                {
                    "content": content,
                    "turn_number": turn_number,
                    "model_id": active_model,
                    "auth_mode": self._auth_mode_for_model(active_model),
                    "estimated_tokens": len(content) // 4,
                    "duration_ms": duration_ms,
                },
            )
            all_events = log_obj.read_all()
            project_thread(all_events, thread_id, self.vault_root)

    def pin_thread(self, thread_id: str, model: str) -> None:
        """Pin a thread to a specific model. Vault-first: appends thread_pin then re-projects."""
        log_obj = EventLog(self._log_path(thread_id))
        if not log_obj.read_all():
            raise ValueError(f"Thread {thread_id!r} not found")
        log_obj.append("thread_pin", {"model": model, "thread_id": thread_id})
        project_thread(log_obj.read_all(), thread_id, self.vault_root)

    def unpin_thread(self, thread_id: str) -> None:
        """Remove a model pin from a thread."""
        log_obj = EventLog(self._log_path(thread_id))
        if not log_obj.read_all():
            raise ValueError(f"Thread {thread_id!r} not found")
        log_obj.append("thread_pin", {"model": None, "thread_id": thread_id})
        project_thread(log_obj.read_all(), thread_id, self.vault_root)

    def list_threads(self) -> list[dict[str, Any]]:
        """List threads from Logs/. Returns [{thread_id, title, updated}, ...]."""
        logs_dir = self.vault_root / "Orchestrator" / "Logs"
        if not logs_dir.exists():
            return []
        threads: list[dict[str, Any]] = []
        for jsonl_path in sorted(logs_dir.glob("*.jsonl")):
            events = EventLog(jsonl_path).read_all()
            if not events:
                continue
            thread_id: str = jsonl_path.stem
            title = ""
            updated = events[-1].timestamp
            for evt in events:
                if evt.event_type == "turn_user":
                    thread_id = str(evt.payload.get("thread_id", jsonl_path.stem))
                    title = str(evt.payload.get("content", ""))[:64]
                    break
            threads.append({"thread_id": thread_id, "title": title, "updated": updated})
        return threads

    def load_thread(self, thread_id: str) -> list[dict[str, Any]]:
        """Load a thread's events as a serialisable list for the plugin."""
        log_obj = EventLog(self._log_path(thread_id))
        return [
            {"event_type": e.event_type, "timestamp": e.timestamp, "payload": e.payload}
            for e in log_obj.read_all()
        ]

    # ------------------------------------------------------------------
    # Project hub methods (Phase 3A)
    # ------------------------------------------------------------------

    def list_projects(self) -> list[dict[str, Any]]:
        """Return indexed project notes sorted: active first, then last_indexed desc."""
        _status_rank = {"active": 0, "stale": 1, "archived": 2}

        def _sort_key(note: Any) -> tuple[int, object]:
            rank = _status_rank.get(note.status, 3)
            ts = note.last_indexed or datetime.min.replace(tzinfo=UTC)
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=UTC)
            # Negate datetime for descending within same status bucket
            return (rank, datetime.max.replace(tzinfo=UTC) - ts)

        notes = read_project_notes(self.vault_root)
        notes.sort(key=_sort_key)
        return [n.model_dump(mode="json") for n in notes]

    def discover_projects(self) -> list[dict[str, Any]]:
        """Run discover() and return candidates that are not yet indexed."""
        cfg = self._projects_config
        candidates = discover(cfg.scan_root, ignore=list(cfg.ignore))
        indexed_slugs = {n.slug for n in read_project_notes(self.vault_root)}
        return [
            {
                "name": c.name,
                "slug": c.slug,
                "path": c.path,
                "languages": c.languages,
                "has_readme": c.has_readme,
                "file_tree": c.file_tree[:20],
                "markers": c.markers,
                "inferred_status": c.inferred_status,
                "last_modified": c.last_modified,
                "git": c.git,
            }
            for c in candidates
            if c.slug not in indexed_slugs
        ]

    async def index_projects(self, slugs: list[str]) -> list[dict[str, Any]]:
        """Summarize + store each slug.  Returns per-slug ok/error results."""
        cfg = self._projects_config
        all_candidates = discover(cfg.scan_root, ignore=list(cfg.ignore))
        by_slug = {c.slug: c for c in all_candidates}

        results: list[dict[str, Any]] = []
        for slug in slugs:
            candidate = by_slug.get(slug)
            if candidate is None:
                results.append({"slug": slug, "ok": False, "error": "not found in scan"})
                continue
            try:
                note = await summarize(candidate)
                write_project_note(self.vault_root, note)
                results.append({"slug": slug, "ok": True, "note": note.model_dump(mode="json")})
            except Exception as exc:  # noqa: BLE001
                log.exception("index_projects: failed for %s", slug)
                results.append({"slug": slug, "ok": False, "error": str(exc)})
        return results

    async def add_project_manual(self, path: str) -> dict[str, Any]:
        """Summarize + store a project at an arbitrary path.

        Bypasses discover()'s marker-based filtering entirely — the caller is
        explicitly vouching for the folder being a project. Not restricted to
        ``scan_root``: this is the escape hatch for projects discovery can't
        find or that live outside it.

        Raises:
            ValueError: path does not exist, is not a directory, or its slug
                collides with an already-indexed project at a different path.
        """
        folder = Path(path)
        if not folder.is_dir():
            raise ValueError(f"Path does not exist or is not a directory: {path}")

        resolved_path = str(folder.resolve())
        base_slug = slugify(folder.name)
        existing = read_project_note(self.vault_root, base_slug)
        if existing is not None and existing.path != resolved_path:
            raise ValueError(
                f"A different project is already indexed as {base_slug!r} "
                f"(path: {existing.path!r}). Rename the folder or remove the "
                "existing project first."
            )

        candidate = build_candidate(folder, slug=base_slug)
        note = await summarize(candidate)
        write_project_note(self.vault_root, note)
        return note.model_dump(mode="json")

    def project_detail(self, slug: str) -> dict[str, Any]:
        """Assemble the detail view: note + raw README + fresh file tree + git.

        Reads the project folder fresh so the tree/README/git reflect current
        state, not what was captured at index time.

        Raises:
            KeyError: slug not found in indexed notes.
        """
        note = read_project_note(self.vault_root, slug)
        if note is None:
            raise KeyError(slug)

        folder = Path(note.path)
        readme: str | None = None
        file_tree: list[str] = []
        git: dict[str, str | bool | None] | None = None
        if folder.is_dir():
            has_readme, readme_path = _find_readme(folder)
            if has_readme and readme_path:
                try:
                    readme = Path(readme_path).read_text(encoding="utf-8", errors="replace")
                    readme = readme[:200_000]  # sanity cap for pathological files
                except OSError:
                    readme = None
            file_tree = _build_file_tree(folder)
            git = _get_git_info(folder)

        return {
            "note": note.model_dump(mode="json"),
            "readme": readme,
            "file_tree": file_tree,
            "git": git,
            "folder_exists": folder.is_dir(),
        }

    def remove_project(self, slug: str) -> bool:
        """Delete the project note (not the folder).  Returns False if unknown slug."""
        return delete_project_note(self.vault_root, slug)

    def update_project_run_command(self, slug: str, run_command: str) -> dict[str, Any]:
        """Persist a user-set run_command on an existing project note.

        Raises:
            KeyError: slug not found in indexed notes.
        """
        updated = update_run_command(self.vault_root, slug, run_command)
        if updated is None:
            raise KeyError(slug)
        return updated.model_dump(mode="json")

    async def start_project(self, slug: str) -> dict[str, Any]:
        """Start the project's run_command as a subprocess (cwd=project path).

        Idempotent if already running (returns current status, does not
        double-spawn). Process state is in-memory only — it does not survive
        an orchestrator restart.

        Raises:
            KeyError: slug not found in indexed notes.
            ValueError: no run_command configured for this project.
        """
        note = read_project_note(self.vault_root, slug)
        if note is None:
            raise KeyError(slug)
        return await self._runner.start(slug, note.run_command or "", Path(note.path))

    async def stop_project(self, slug: str) -> dict[str, Any]:
        """Terminate the project's running process. Idempotent if not running.

        Raises:
            KeyError: slug not found in indexed notes.
        """
        note = read_project_note(self.vault_root, slug)
        if note is None:
            raise KeyError(slug)
        return await self._runner.stop(slug)

    def project_status(self, slug: str) -> dict[str, Any]:
        """Return {running, pid, output} for the project's tracked process.

        Raises:
            KeyError: slug not found in indexed notes.
        """
        note = read_project_note(self.vault_root, slug)
        if note is None:
            raise KeyError(slug)
        return self._runner.status(slug)

    def open_project(self, slug: str, target: str) -> None:
        """Open the project in VS Code ('vscode') or Finder ('finder').

        Raises:
            KeyError: slug not found in indexed notes.
            ValueError: target is not 'vscode' or 'finder'.
            PermissionError: project path is outside scan_root.
        """
        note = read_project_note(self.vault_root, slug)
        if note is None:
            raise KeyError(slug)

        project_path = Path(note.path)
        scan_root = self._projects_config.scan_root
        if not project_path.is_relative_to(scan_root):
            raise PermissionError(
                f"Project path {project_path} is outside scan_root {scan_root}"
            )

        if target == "vscode":
            subprocess.Popen(["code", str(project_path)])
        elif target == "finder":
            subprocess.Popen(["open", str(project_path)])
        else:
            raise ValueError(f"Unknown target {target!r}. Must be 'vscode' or 'finder'.")

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _ensure_vault_layout(self) -> None:
        orch_root = self.vault_root / "Orchestrator"
        for subdir in ("Live", "Logs", "Config", "Handovers", "Archive", "Index", "Conflicts"):
            d = orch_root / subdir
            d.mkdir(parents=True, exist_ok=True)
            gitkeep = d / ".gitkeep"
            if not gitkeep.exists() and not any(d.iterdir()):
                gitkeep.touch()
        sentinel = orch_root / "_DO_NOT_EDIT.md"
        if not sentinel.exists():
            sentinel.write_text(_SENTINEL_TEXT, encoding="utf-8")

    def _log_path(self, thread_id: str) -> Path:
        id_short = thread_id.replace("-", "")[:16]
        return self.vault_root / "Orchestrator" / "Logs" / f"{id_short}.jsonl"

    def _load_history(self, log_obj: EventLog) -> list[NexusMessage]:
        messages: list[NexusMessage] = []
        for evt in log_obj.read_all():
            if evt.event_type == "turn_user":
                messages.append(
                    NexusMessage(role="user", content=str(evt.payload.get("content", "")))
                )
            elif evt.event_type == "turn_assistant":
                messages.append(
                    NexusMessage(
                        role="assistant", content=str(evt.payload.get("content", ""))
                    )
                )
        return messages

    def _next_turn_number(self, history: list[NexusMessage]) -> int:
        return sum(1 for m in history if m.role == "user") + 1

    def _load_thread_yaml(self, thread_id: str) -> dict[str, Any] | None:
        """Read the thread's projected markdown frontmatter, if the file exists."""
        live_dir = self.vault_root / "Orchestrator" / "Live"
        if not live_dir.exists():
            return None
        id_short = thread_id.replace("-", "")[:8]
        for path in live_dir.glob(f"*_thread-{id_short}_*.md"):
            try:
                text = path.read_text(encoding="utf-8")
                if not text.startswith("---\n"):
                    continue
                end = text.index("\n---\n", 4)
                result = yaml.safe_load(text[4:end])
                return result if isinstance(result, dict) else {}
            except (ValueError, yaml.YAMLError, OSError):
                pass
        return None

    def _derive_live_filename(
        self,
        thread_id: str,
        log_obj: EventLog,
        pending_user_message: str | None = None,
    ) -> str:
        """Compute the filename project_thread will write for this thread.

        ``pending_user_message`` is the current turn's message, used as the
        title seed when the thread has no saved turn_user yet — this runs
        before the save-worthiness filter decides whether the current turn
        (and its turn_user event) will actually be persisted.
        """
        events = log_obj.read_all()
        first_user = next((e for e in events if e.event_type == "turn_user"), None)
        if first_user is not None:
            title_seed = first_user.payload.get("content", "")[:200]
        else:
            title_seed = (pending_user_message or "")[:200]
        id_short = thread_id.replace("-", "")[:8]
        title = kebab_title(title_seed) or f"untitled-{id_short}"
        created = datetime.fromisoformat(events[0].timestamp) if events else datetime.now(UTC)
        return thread_filename(created, thread_id, title)

    def _auth_mode_for_model(self, model_id: str) -> str:
        """Heuristic mapping from model string to auth_mode."""
        if "claude-" in model_id:
            return "subscription"
        if "/" in model_id:
            return "api_key_free_tier"
        return "local"
