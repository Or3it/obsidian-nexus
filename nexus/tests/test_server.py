"""Tests for orchestrator/server.py — Orchestrator is mocked."""
import json
from collections.abc import AsyncGenerator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest
from starlette.testclient import TestClient

from orchestrator.server import build_app
from orchestrator.types import HealthStatus, StyleProfile

_CASCADE_YAML = """\
schema_version: 2
rungs:
  - position: 1
    adapter: anthropic
    auth_mode: subscription
    use_ruflo: false
    models:
      default: claude-sonnet-4-6
"""

# ---------------------------------------------------------------------------
# Mock orchestrator
# ---------------------------------------------------------------------------


class _MockOrchestrator:
    """Default: streams a single 'pong' token then done."""

    _stream_events: list[dict[str, Any]] = [
        {"type": "token", "text": "pong", "is_final": False},
        {"type": "token", "text": "", "is_final": True},
    ]

    def new_thread_id(self) -> str:
        return "aaaabbbb-0000-7000-8000-000000000001"

    def __init__(self) -> None:
        self.last_turn_kwargs: dict[str, Any] = {}

    async def send_turn_stream(
        self,
        thread_id: str,
        user_message: str,
        style_profile: StyleProfile | None = None,
        mode: str = "auto",
        include_projects: bool = False,
    ) -> AsyncGenerator[dict[str, Any], None]:
        self.last_turn_kwargs = {"mode": mode, "include_projects": include_projects}
        for ev in self._stream_events:
            yield ev

    def list_threads(self) -> list[dict[str, str]]:
        return [{"thread_id": "aaaabbbb", "title": "hi", "updated": "2026-05-15"}]

    def load_thread(self, thread_id: str) -> list[dict[str, object]]:
        return [
            {"event_type": "turn_user", "timestamp": "2026-05-15", "payload": {"content": "hi"}}
        ]

    def pin_thread(self, thread_id: str, model: str) -> None:
        pass

    def unpin_thread(self, thread_id: str) -> None:
        pass

    # cascade attribute for /health
    class _FakeCascade:
        async def health_check_all(self) -> dict[int, HealthStatus]:
            return {1: HealthStatus(healthy=True, last_checked=datetime.now(UTC))}

        def pool_health_status(self) -> None:
            return None  # legacy: no pool

    cascade = _FakeCascade()


class _MockOrchestratorWithHandover(_MockOrchestrator):
    _stream_events = [
        {"type": "token", "text": "err", "is_final": False},
        {
            "type": "handover",
            "from_rung": 1,
            "to_rung": 2,
            "from_model": "claude-sonnet-4-6",
            "to_model": "minimax/minimax-m1",
            "trigger": "HARD_FAIL",
        },
        {"type": "token", "text": "ok", "is_final": False},
        {"type": "token", "text": "", "is_final": True},
    ]


class _MockOrchestratorWithError(_MockOrchestrator):
    _stream_events = [
        {"type": "error", "message": "all rungs exhausted", "terminal": True},
    ]


class _MockOrchestratorUnhealthy(_MockOrchestrator):
    class _FakeCascade:
        async def health_check_all(self) -> dict[int, HealthStatus]:
            return {
                1: HealthStatus(healthy=False, last_checked=datetime.now(UTC), detail="down"),
                2: HealthStatus(healthy=True, last_checked=datetime.now(UTC)),
            }

        def pool_health_status(self) -> None:
            return None

    cascade = _FakeCascade()


class _MockOrchestratorWithPool(_MockOrchestrator):
    """Schema-3 pool config: /health must include a 'pool' array."""

    class _FakeCascade:
        async def health_check_all(self) -> dict[int, HealthStatus]:
            return {1: HealthStatus(healthy=True, last_checked=datetime.now(UTC))}

        def pool_health_status(self) -> list[dict]:  # type: ignore[type-arg]
            return [
                {
                    "model": "openai/gpt-oss-120b:free",
                    "state": "live",
                    "last_success": "2026-05-30T12:00:00+00:00",
                },
                {
                    "model": "deepseek/deepseek-v4-flash:free",
                    "state": "dead",
                    "until": "2026-05-30T13:00:00+00:00",
                    "reason": "Provider returned error",
                    "failures": 2,
                },
                {
                    "model": "minimax/minimax-m2.5:free",
                    "state": "untested",
                },
            ]

    cascade = _FakeCascade()


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _make_client(tmp_path: Path, mock_cls: type = _MockOrchestrator) -> tuple[TestClient, str]:
    config_dir = tmp_path / "Orchestrator" / "Config"
    config_dir.mkdir(parents=True)
    (config_dir / "cascade.yaml").write_text(_CASCADE_YAML, encoding="utf-8")

    with patch("orchestrator.server.Orchestrator", return_value=mock_cls()):
        app = build_app(tmp_path)

    token = (tmp_path / "Orchestrator" / "Config" / "auth.token").read_text().strip()
    return TestClient(app, raise_server_exceptions=True), token


@pytest.fixture
def client_and_token(tmp_path: Path) -> tuple[TestClient, str]:
    return _make_client(tmp_path)


# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------


class TestAuth:
    def test_post_turn_without_auth_returns_401(
        self, client_and_token: tuple[TestClient, str]
    ) -> None:
        client, _ = client_and_token
        resp = client.post("/turn", json={"user_message": "hi"})
        assert resp.status_code == 401

    def test_post_turn_with_bad_token_returns_401(
        self, client_and_token: tuple[TestClient, str]
    ) -> None:
        client, _ = client_and_token
        resp = client.post(
            "/turn",
            json={"user_message": "hi"},
            headers={"Authorization": "Bearer wrong-token"},
        )
        assert resp.status_code == 401


# ---------------------------------------------------------------------------
# /turn — happy path (token stream)
# ---------------------------------------------------------------------------


class TestTurnStream:
    def test_post_turn_streams_sse_events(
        self, client_and_token: tuple[TestClient, str]
    ) -> None:
        client, token = client_and_token
        with client.stream(
            "POST",
            "/turn",
            json={"user_message": "ping"},
            headers={"Authorization": f"Bearer {token}"},
        ) as resp:
            assert resp.status_code == 200
            assert "text/event-stream" in resp.headers["content-type"]
            body = resp.read().decode()

        assert "event: thread" in body
        assert "event: token" in body
        assert "event: done" in body

    def test_token_events_carry_text(
        self, client_and_token: tuple[TestClient, str]
    ) -> None:
        client, token = client_and_token
        with client.stream(
            "POST",
            "/turn",
            json={"user_message": "ping"},
            headers={"Authorization": f"Bearer {token}"},
        ) as resp:
            body = resp.read().decode()

        token_lines = [
            line for line in body.splitlines() if line.startswith("data:") and '"token"' in line
        ]
        payloads = [json.loads(line.removeprefix("data:").strip()) for line in token_lines]
        texts = [p["text"] for p in payloads if p.get("text")]
        assert "pong" in texts


# ---------------------------------------------------------------------------
# /turn — handover mid-stream
# ---------------------------------------------------------------------------


class TestTurnStreamWithHandover:
    def test_handover_event_in_sse(self, tmp_path: Path) -> None:
        client, token = _make_client(tmp_path, _MockOrchestratorWithHandover)
        with client.stream(
            "POST",
            "/turn",
            json={"user_message": "hi"},
            headers={"Authorization": f"Bearer {token}"},
        ) as resp:
            body = resp.read().decode()

        assert "event: handover" in body
        handover_lines = [
            line for line in body.splitlines()
            if line.startswith("data:") and '"handover"' in line
        ]
        assert len(handover_lines) == 1
        payload = json.loads(handover_lines[0].removeprefix("data:").strip())
        assert payload["from_rung"] == 1
        assert payload["to_rung"] == 2
        assert payload["trigger"] == "HARD_FAIL"

    def test_tokens_appear_after_handover(self, tmp_path: Path) -> None:
        client, token = _make_client(tmp_path, _MockOrchestratorWithHandover)
        with client.stream(
            "POST",
            "/turn",
            json={"user_message": "hi"},
            headers={"Authorization": f"Bearer {token}"},
        ) as resp:
            body = resp.read().decode()

        assert "event: token" in body
        assert "event: done" in body


# ---------------------------------------------------------------------------
# /turn — terminal error
# ---------------------------------------------------------------------------


class TestTurnStreamWithError:
    def test_terminal_error_ends_with_done(self, tmp_path: Path) -> None:
        client, token = _make_client(tmp_path, _MockOrchestratorWithError)
        with client.stream(
            "POST",
            "/turn",
            json={"user_message": "hi"},
            headers={"Authorization": f"Bearer {token}"},
        ) as resp:
            body = resp.read().decode()

        assert "event: error" in body
        assert "event: done" in body

    def test_terminal_error_payload(self, tmp_path: Path) -> None:
        client, token = _make_client(tmp_path, _MockOrchestratorWithError)
        with client.stream(
            "POST",
            "/turn",
            json={"user_message": "hi"},
            headers={"Authorization": f"Bearer {token}"},
        ) as resp:
            body = resp.read().decode()

        error_lines = [
            line for line in body.splitlines()
            if line.startswith("data:") and '"error"' in line
        ]
        assert len(error_lines) == 1
        payload = json.loads(error_lines[0].removeprefix("data:").strip())
        assert payload["terminal"] is True
        assert "rungs exhausted" in payload["message"]


# ---------------------------------------------------------------------------
# /threads
# ---------------------------------------------------------------------------


class TestThreadsEndpoint:
    def test_get_threads_returns_list(
        self, client_and_token: tuple[TestClient, str]
    ) -> None:
        client, token = client_and_token
        resp = client.get("/threads", headers={"Authorization": f"Bearer {token}"})
        assert resp.status_code == 200
        data = resp.json()
        assert isinstance(data, list)
        assert len(data) == 1


# ---------------------------------------------------------------------------
# /health — multi-rung structure
# ---------------------------------------------------------------------------


class TestHealth:
    def test_health_returns_status_without_auth(
        self, client_and_token: tuple[TestClient, str]
    ) -> None:
        client, _ = client_and_token
        resp = client.get("/health")
        assert resp.status_code == 200
        body = resp.json()
        assert body["healthy"] is True

    def test_health_includes_rungs(
        self, client_and_token: tuple[TestClient, str]
    ) -> None:
        client, _ = client_and_token
        resp = client.get("/health")
        body = resp.json()
        assert "rungs" in body
        assert "last_checked" in body
        assert 1 in body["rungs"] or "1" in body["rungs"]

    def test_health_false_when_all_rungs_down(self, tmp_path: Path) -> None:
        class _AllDown(_MockOrchestrator):
            class _FakeCascade:
                async def health_check_all(self) -> dict[int, HealthStatus]:
                    return {
                        1: HealthStatus(
                            healthy=False, last_checked=datetime.now(UTC), detail="down"
                        )
                    }

                def pool_health_status(self) -> None:
                    return None

            cascade = _FakeCascade()

        client, _ = _make_client(tmp_path, _AllDown)
        resp = client.get("/health")
        assert resp.json()["healthy"] is False

    def test_health_true_if_any_rung_healthy(self, tmp_path: Path) -> None:
        client, _ = _make_client(tmp_path, _MockOrchestratorUnhealthy)
        resp = client.get("/health")
        assert resp.json()["healthy"] is True


# ---------------------------------------------------------------------------
# /threads/{thread_id}/pin
# ---------------------------------------------------------------------------


class _MockOrchestratorPinError(_MockOrchestrator):
    def pin_thread(self, thread_id: str, model: str) -> None:
        raise ValueError("thread not found")

    def unpin_thread(self, thread_id: str) -> None:
        raise ValueError("thread not found")


class TestPinEndpoint:
    def test_post_pin_returns_204(self, tmp_path: Path) -> None:
        client, token = _make_client(tmp_path)
        resp = client.post(
            "/threads/some-thread-id/pin",
            json={"model": "claude-opus-4-7"},
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp.status_code == 204

    def test_post_pin_requires_auth(self, tmp_path: Path) -> None:
        client, _ = _make_client(tmp_path)
        resp = client.post("/threads/some-thread-id/pin", json={"model": "claude-opus-4-7"})
        assert resp.status_code == 401

    def test_post_pin_unknown_thread_returns_404(self, tmp_path: Path) -> None:
        client, token = _make_client(tmp_path, _MockOrchestratorPinError)
        resp = client.post(
            "/threads/missing/pin",
            json={"model": "claude-opus-4-7"},
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp.status_code == 404

    def test_delete_pin_returns_204(self, tmp_path: Path) -> None:
        client, token = _make_client(tmp_path)
        resp = client.delete(
            "/threads/some-thread-id/pin",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp.status_code == 204

    def test_delete_pin_requires_auth(self, tmp_path: Path) -> None:
        client, _ = _make_client(tmp_path)
        resp = client.delete("/threads/some-thread-id/pin")
        assert resp.status_code == 401

    def test_delete_pin_unknown_thread_returns_404(self, tmp_path: Path) -> None:
        client, token = _make_client(tmp_path, _MockOrchestratorPinError)
        resp = client.delete(
            "/threads/missing/pin",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp.status_code == 404


# ---------------------------------------------------------------------------
# /health — pool ModelHealth extension (P2c-T06)
# ---------------------------------------------------------------------------


class TestHealthPool:
    def test_pool_key_present_for_pool_config(self, tmp_path: Path) -> None:
        """/health must include a 'pool' array when the cascade uses a pool."""
        client, _ = _make_client(tmp_path, _MockOrchestratorWithPool)
        resp = client.get("/health")
        assert resp.status_code == 200
        body = resp.json()
        assert "pool" in body
        assert isinstance(body["pool"], list)
        assert len(body["pool"]) == 3

    def test_pool_entry_shape(self, tmp_path: Path) -> None:
        """Each pool entry has model + state; dead entries include until and reason."""
        client, _ = _make_client(tmp_path, _MockOrchestratorWithPool)
        body = client.get("/health").json()
        pool = body["pool"]

        live = next(e for e in pool if e["state"] == "live")
        assert live["model"] == "openai/gpt-oss-120b:free"

        dead = next(e for e in pool if e["state"] == "dead")
        assert dead["model"] == "deepseek/deepseek-v4-flash:free"
        assert "until" in dead
        assert "reason" in dead
        assert dead["failures"] == 2

        untested = next(e for e in pool if e["state"] == "untested")
        assert untested["model"] == "minimax/minimax-m2.5:free"

    def test_no_pool_key_for_legacy_schema2(
        self, client_and_token: tuple[TestClient, str]
    ) -> None:
        """Legacy schema-2 config: /health must NOT include a 'pool' key."""
        client, _ = client_and_token
        body = client.get("/health").json()
        assert "pool" not in body


# ---------------------------------------------------------------------------
# Project hub endpoints (Phase 3A)
# ---------------------------------------------------------------------------


class _MockOrchestratorWithProjects(_MockOrchestrator):
    """Mock with project hub methods."""

    _indexed: list[dict] = [
        {
            "name": "Worship Proj",
            "slug": "worship-proj",
            "status": "active",
            "stack": ["python"],
            "category": "worship",
            "path": "/tmp/docs/worship-proj",
            "last_indexed": "2026-06-01T00:00:00+00:00",
            "discovered_at": "2026-06-01T00:00:00+00:00",
            "run_command": None,
            "preview": None,
            "summary": "A worship projector.",
        }
    ]

    _candidates: list[dict] = [
        {
            "name": "New Proj",
            "slug": "new-proj",
            "path": "/tmp/docs/new-proj",
            "languages": ["python"],
            "has_readme": False,
            "file_tree": [],
            "markers": ["pyproject.toml"],
            "inferred_status": "active",
            "last_modified": "2026-06-01T00:00:00+00:00",
            "git": None,
        }
    ]

    def list_projects(self) -> list[dict]:
        return list(self._indexed)

    def discover_projects(self) -> list[dict]:
        return list(self._candidates)

    async def index_projects(self, slugs: list[str]) -> list[dict]:
        return [{"slug": s, "ok": True, "note": {"slug": s}} for s in slugs]

    async def add_project_manual(self, path: str) -> dict:
        if path == "/does/not/exist":
            raise ValueError(f"Path does not exist or is not a directory: {path}")
        if path == "/already/indexed/elsewhere":
            raise ValueError("A different project is already indexed as 'elsewhere'")
        return {"slug": "manual-proj", "path": path, "summary": "A manually added project."}

    def remove_project(self, slug: str) -> bool:
        return slug == "worship-proj"

    def open_project(self, slug: str, target: str) -> None:
        if slug == "missing":
            raise KeyError(slug)
        if target not in ("vscode", "finder"):
            raise ValueError(f"Unknown target {target!r}")

    def project_detail(self, slug: str) -> dict:
        if slug != "worship-proj":
            raise KeyError(slug)
        return {
            "note": self._indexed[0],
            "readme": "# Worship Proj\n\nProjects lyrics.",
            "file_tree": ["app.py", "lyrics/", "lyrics/amazing-grace.txt"],
            "git": {"branch": "main", "dirty": False},
            "folder_exists": True,
        }

    def update_project_run_command(self, slug: str, run_command: str) -> dict:
        if slug != "worship-proj":
            raise KeyError(slug)
        return {**self._indexed[0], "run_command": run_command}

    async def start_project(self, slug: str) -> dict:
        if slug == "missing":
            raise KeyError(slug)
        if slug == "no-run-command":
            raise ValueError("no run_command configured for this project")
        return {"running": True, "pid": 4242, "output": []}

    async def stop_project(self, slug: str) -> dict:
        if slug == "missing":
            raise KeyError(slug)
        return {"running": False, "pid": None, "output": ["done"]}

    def project_status(self, slug: str) -> dict:
        if slug == "missing":
            raise KeyError(slug)
        return {"running": True, "pid": 4242, "output": ["line1", "line2"]}


class TestProjectEndpoints:
    # --- GET /projects ---

    def test_get_projects_returns_list(self, tmp_path: Path) -> None:
        client, token = _make_client(tmp_path, _MockOrchestratorWithProjects)
        resp = client.get("/projects", headers={"Authorization": f"Bearer {token}"})
        assert resp.status_code == 200
        data = resp.json()
        assert isinstance(data, list)
        assert data[0]["slug"] == "worship-proj"

    def test_get_projects_requires_auth(self, tmp_path: Path) -> None:
        client, _ = _make_client(tmp_path, _MockOrchestratorWithProjects)
        assert client.get("/projects").status_code == 401

    # --- POST /projects/discover ---

    def test_discover_returns_candidates(self, tmp_path: Path) -> None:
        client, token = _make_client(tmp_path, _MockOrchestratorWithProjects)
        resp = client.post(
            "/projects/discover",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert isinstance(data, list)
        assert data[0]["slug"] == "new-proj"

    def test_discover_requires_auth(self, tmp_path: Path) -> None:
        client, _ = _make_client(tmp_path, _MockOrchestratorWithProjects)
        assert client.post("/projects/discover").status_code == 401

    # --- POST /projects/index ---

    def test_index_returns_per_slug_results(self, tmp_path: Path) -> None:
        client, token = _make_client(tmp_path, _MockOrchestratorWithProjects)
        resp = client.post(
            "/projects/index",
            json={"slugs": ["new-proj"]},
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert isinstance(data, list)
        assert data[0]["slug"] == "new-proj"
        assert data[0]["ok"] is True

    def test_index_requires_auth(self, tmp_path: Path) -> None:
        client, _ = _make_client(tmp_path, _MockOrchestratorWithProjects)
        assert (
            client.post("/projects/index", json={"slugs": ["x"]}).status_code == 401
        )

    # --- POST /projects/manual ---

    def test_manual_add_returns_created_note(self, tmp_path: Path) -> None:
        client, token = _make_client(tmp_path, _MockOrchestratorWithProjects)
        resp = client.post(
            "/projects/manual",
            json={"path": "/Users/x/Documents/some-project"},
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["slug"] == "manual-proj"
        assert data["path"] == "/Users/x/Documents/some-project"

    def test_manual_add_bad_path_returns_400(self, tmp_path: Path) -> None:
        client, token = _make_client(tmp_path, _MockOrchestratorWithProjects)
        resp = client.post(
            "/projects/manual",
            json={"path": "/does/not/exist"},
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp.status_code == 400
        assert "does not exist" in resp.json()["detail"]

    def test_manual_add_slug_collision_returns_400(self, tmp_path: Path) -> None:
        client, token = _make_client(tmp_path, _MockOrchestratorWithProjects)
        resp = client.post(
            "/projects/manual",
            json={"path": "/already/indexed/elsewhere"},
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp.status_code == 400
        assert "already indexed" in resp.json()["detail"]

    def test_manual_add_requires_auth(self, tmp_path: Path) -> None:
        client, _ = _make_client(tmp_path, _MockOrchestratorWithProjects)
        resp = client.post("/projects/manual", json={"path": "/tmp/x"})
        assert resp.status_code == 401

    # --- DELETE /projects/{slug} ---

    def test_delete_existing_returns_200(self, tmp_path: Path) -> None:
        client, token = _make_client(tmp_path, _MockOrchestratorWithProjects)
        resp = client.delete(
            "/projects/worship-proj",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp.status_code == 200
        assert resp.json()["ok"] is True

    def test_delete_missing_returns_404(self, tmp_path: Path) -> None:
        client, token = _make_client(tmp_path, _MockOrchestratorWithProjects)
        resp = client.delete(
            "/projects/no-such",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp.status_code == 404

    def test_delete_requires_auth(self, tmp_path: Path) -> None:
        client, _ = _make_client(tmp_path, _MockOrchestratorWithProjects)
        assert client.delete("/projects/worship-proj").status_code == 401

    # --- POST /projects/{slug}/open ---

    def test_open_vscode_returns_ok(self, tmp_path: Path) -> None:
        client, token = _make_client(tmp_path, _MockOrchestratorWithProjects)
        resp = client.post(
            "/projects/worship-proj/open",
            json={"target": "vscode"},
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp.status_code == 200
        assert resp.json()["ok"] is True

    def test_open_finder_returns_ok(self, tmp_path: Path) -> None:
        client, token = _make_client(tmp_path, _MockOrchestratorWithProjects)
        resp = client.post(
            "/projects/worship-proj/open",
            json={"target": "finder"},
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp.status_code == 200

    def test_open_unknown_slug_returns_404(self, tmp_path: Path) -> None:
        client, token = _make_client(tmp_path, _MockOrchestratorWithProjects)
        resp = client.post(
            "/projects/missing/open",
            json={"target": "vscode"},
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp.status_code == 404

    def test_open_bad_target_returns_400(self, tmp_path: Path) -> None:
        client, token = _make_client(tmp_path, _MockOrchestratorWithProjects)
        resp = client.post(
            "/projects/worship-proj/open",
            json={"target": "emacs"},
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp.status_code == 400

    def test_open_requires_auth(self, tmp_path: Path) -> None:
        client, _ = _make_client(tmp_path, _MockOrchestratorWithProjects)
        assert (
            client.post(
                "/projects/worship-proj/open", json={"target": "vscode"}
            ).status_code
            == 401
        )

    # --- GET /projects/{slug}/detail ---

    def test_detail_returns_note_readme_tree_git(self, tmp_path: Path) -> None:
        client, token = _make_client(tmp_path, _MockOrchestratorWithProjects)
        resp = client.get(
            "/projects/worship-proj/detail",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["note"]["slug"] == "worship-proj"
        assert body["readme"].startswith("# Worship Proj")
        assert "app.py" in body["file_tree"]
        assert body["git"]["branch"] == "main"

    def test_detail_unknown_slug_returns_404(self, tmp_path: Path) -> None:
        client, token = _make_client(tmp_path, _MockOrchestratorWithProjects)
        resp = client.get(
            "/projects/no-such/detail",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp.status_code == 404

    def test_detail_requires_auth(self, tmp_path: Path) -> None:
        client, _ = _make_client(tmp_path, _MockOrchestratorWithProjects)
        assert client.get("/projects/worship-proj/detail").status_code == 401

    # --- PATCH /projects/{slug} ---

    def test_patch_run_command_persists(self, tmp_path: Path) -> None:
        client, token = _make_client(tmp_path, _MockOrchestratorWithProjects)
        resp = client.patch(
            "/projects/worship-proj",
            json={"run_command": "python app.py"},
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp.status_code == 200
        assert resp.json()["run_command"] == "python app.py"

    def test_patch_unknown_slug_returns_404(self, tmp_path: Path) -> None:
        client, token = _make_client(tmp_path, _MockOrchestratorWithProjects)
        resp = client.patch(
            "/projects/no-such",
            json={"run_command": "python app.py"},
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp.status_code == 404

    def test_patch_requires_auth(self, tmp_path: Path) -> None:
        client, _ = _make_client(tmp_path, _MockOrchestratorWithProjects)
        resp = client.patch("/projects/worship-proj", json={"run_command": "x"})
        assert resp.status_code == 401

    # --- POST /projects/{slug}/start ---

    def test_start_returns_running_status(self, tmp_path: Path) -> None:
        client, token = _make_client(tmp_path, _MockOrchestratorWithProjects)
        resp = client.post(
            "/projects/worship-proj/start",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp.status_code == 200
        assert resp.json() == {"running": True, "pid": 4242, "output": []}

    def test_start_unknown_slug_returns_404(self, tmp_path: Path) -> None:
        client, token = _make_client(tmp_path, _MockOrchestratorWithProjects)
        resp = client.post(
            "/projects/missing/start",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp.status_code == 404

    def test_start_without_run_command_returns_400(self, tmp_path: Path) -> None:
        client, token = _make_client(tmp_path, _MockOrchestratorWithProjects)
        resp = client.post(
            "/projects/no-run-command/start",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp.status_code == 400

    def test_start_requires_auth(self, tmp_path: Path) -> None:
        client, _ = _make_client(tmp_path, _MockOrchestratorWithProjects)
        assert client.post("/projects/worship-proj/start").status_code == 401

    # --- POST /projects/{slug}/stop ---

    def test_stop_returns_status(self, tmp_path: Path) -> None:
        client, token = _make_client(tmp_path, _MockOrchestratorWithProjects)
        resp = client.post(
            "/projects/worship-proj/stop",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp.status_code == 200
        assert resp.json()["running"] is False

    def test_stop_unknown_slug_returns_404(self, tmp_path: Path) -> None:
        client, token = _make_client(tmp_path, _MockOrchestratorWithProjects)
        resp = client.post(
            "/projects/missing/stop",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp.status_code == 404

    def test_stop_requires_auth(self, tmp_path: Path) -> None:
        client, _ = _make_client(tmp_path, _MockOrchestratorWithProjects)
        assert client.post("/projects/worship-proj/stop").status_code == 401

    # --- GET /projects/{slug}/status ---

    def test_status_returns_running_pid_output(self, tmp_path: Path) -> None:
        client, token = _make_client(tmp_path, _MockOrchestratorWithProjects)
        resp = client.get(
            "/projects/worship-proj/status",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["running"] is True
        assert body["pid"] == 4242
        assert body["output"] == ["line1", "line2"]

    def test_status_unknown_slug_returns_404(self, tmp_path: Path) -> None:
        client, token = _make_client(tmp_path, _MockOrchestratorWithProjects)
        resp = client.get(
            "/projects/missing/status",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp.status_code == 404

    def test_status_requires_auth(self, tmp_path: Path) -> None:
        client, _ = _make_client(tmp_path, _MockOrchestratorWithProjects)
        assert client.get("/projects/worship-proj/status").status_code == 401


class TestTurnModePassthrough:
    def test_mode_and_include_projects_reach_orchestrator(
        self, tmp_path: Path
    ) -> None:
        mock = _MockOrchestrator()
        config_dir = tmp_path / "Orchestrator" / "Config"
        config_dir.mkdir(parents=True)
        (config_dir / "cascade.yaml").write_text(_CASCADE_YAML, encoding="utf-8")
        with patch("orchestrator.server.Orchestrator", return_value=mock):
            app = build_app(tmp_path)
        token = (config_dir / "auth.token").read_text().strip()
        client = TestClient(app)

        resp = client.post(
            "/turn",
            json={"user_message": "hi", "mode": "light", "include_projects": True},
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp.status_code == 200
        resp.read()
        assert mock.last_turn_kwargs == {"mode": "light", "include_projects": True}

    def test_invalid_mode_rejected(self, tmp_path: Path) -> None:
        client, token = _make_client(tmp_path)
        resp = client.post(
            "/turn",
            json={"user_message": "hi", "mode": "turbo"},
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp.status_code == 422

    def test_defaults_are_auto_and_no_projects(self, tmp_path: Path) -> None:
        mock = _MockOrchestrator()
        config_dir = tmp_path / "Orchestrator" / "Config"
        config_dir.mkdir(parents=True)
        (config_dir / "cascade.yaml").write_text(_CASCADE_YAML, encoding="utf-8")
        with patch("orchestrator.server.Orchestrator", return_value=mock):
            app = build_app(tmp_path)
        token = (config_dir / "auth.token").read_text().strip()
        client = TestClient(app)

        resp = client.post(
            "/turn",
            json={"user_message": "hi"},
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp.status_code == 200
        resp.read()
        assert mock.last_turn_kwargs == {"mode": "auto", "include_projects": False}


class TestNexusHomeWebApp:
    def test_app_page_serves_html_with_token(self, tmp_path: Path) -> None:
        client, token = _make_client(tmp_path)
        resp = client.get("/app")
        assert resp.status_code == 200
        assert resp.headers["content-type"].startswith("text/html")
        assert token in resp.text            # token templated in
        assert "{{NEXUS_TOKEN}}" not in resp.text
        assert "Nexus Home" in resp.text

    def test_static_assets_served(self, tmp_path: Path) -> None:
        client, _ = _make_client(tmp_path)
        css = client.get("/app/static/style.css")
        js = client.get("/app/static/app.js")
        assert css.status_code == 200
        assert "backdrop-filter" in css.text  # liquid glass present
        assert js.status_code == 200
        assert "NEXUS_TOKEN" in js.text
