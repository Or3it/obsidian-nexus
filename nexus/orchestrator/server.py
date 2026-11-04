"""FastAPI application: exposes the orchestrator over localhost HTTP/SSE."""
import json
import logging
import secrets
from collections.abc import AsyncGenerator
from datetime import UTC, datetime
from pathlib import Path

from fastapi import FastAPI, Header, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from .orchestrator import Orchestrator
from .types import StyleProfile, TurnMode

log = logging.getLogger(__name__)

_WEBAPP_DIR = Path(__file__).parent / "webapp"


class SendTurnPayload(BaseModel):
    thread_id: str | None = None
    user_message: str
    style_profile: StyleProfile | None = None
    mode: TurnMode = "auto"
    include_projects: bool = False


class PinPayload(BaseModel):
    model: str


class IndexPayload(BaseModel):
    slugs: list[str]


class OpenPayload(BaseModel):
    target: str


class RunCommandPayload(BaseModel):
    run_command: str


class ManualAddPayload(BaseModel):
    path: str


def build_app(vault_root: Path) -> FastAPI:
    orchestrator = Orchestrator(vault_root)
    token = _load_or_create_token(vault_root)
    app = FastAPI(title="Obsidian Nexus", version="0.1.0")
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["app://obsidian.md", "capacitor://localhost", "http://localhost"],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    def _auth(authorization: str | None) -> None:
        if not authorization or authorization != f"Bearer {token}":
            raise HTTPException(status_code=401, detail="missing/invalid auth")

    @app.post("/turn")
    async def post_turn(
        payload: SendTurnPayload,
        authorization: str | None = Header(default=None),
    ) -> StreamingResponse:
        _auth(authorization)
        thread_id = payload.thread_id or orchestrator.new_thread_id()

        async def event_stream() -> AsyncGenerator[str, None]:
            yield f'event: thread\ndata: {{"thread_id": "{thread_id}"}}\n\n'
            try:
                async for ev in orchestrator.send_turn_stream(
                    thread_id,
                    payload.user_message,
                    payload.style_profile,
                    mode=payload.mode,
                    include_projects=payload.include_projects,
                ):
                    yield (
                        f'event: {ev["type"]}\n'
                        f'data: {json.dumps(ev, separators=(",", ":"))}\n\n'
                    )
                    if ev["type"] == "error" and ev.get("terminal"):
                        break
            except Exception:
                log.exception("Unhandled error in /turn stream")
                err = json.dumps(
                    {"type": "error", "message": "internal server error", "terminal": True}
                )
                yield f"event: error\ndata: {err}\n\n"
            yield "event: done\ndata: {}\n\n"

        return StreamingResponse(event_stream(), media_type="text/event-stream")

    @app.get("/threads")
    def list_threads(authorization: str | None = Header(default=None)) -> object:
        _auth(authorization)
        return orchestrator.list_threads()

    @app.get("/threads/{thread_id}")
    def get_thread(
        thread_id: str,
        authorization: str | None = Header(default=None),
    ) -> object:
        _auth(authorization)
        return orchestrator.load_thread(thread_id)

    @app.post("/threads/{thread_id}/pin", status_code=204)
    def pin_thread(
        thread_id: str,
        payload: PinPayload,
        authorization: str | None = Header(default=None),
    ) -> None:
        _auth(authorization)
        try:
            orchestrator.pin_thread(thread_id, payload.model)
        except ValueError:
            raise HTTPException(status_code=404, detail="thread not found")

    @app.delete("/threads/{thread_id}/pin", status_code=204)
    def unpin_thread(
        thread_id: str,
        authorization: str | None = Header(default=None),
    ) -> None:
        _auth(authorization)
        try:
            orchestrator.unpin_thread(thread_id)
        except ValueError:
            raise HTTPException(status_code=404, detail="thread not found")

    # ------------------------------------------------------------------
    # Project hub endpoints (Phase 3A)
    # ------------------------------------------------------------------

    @app.get("/projects")
    def list_projects(authorization: str | None = Header(default=None)) -> object:
        _auth(authorization)
        return orchestrator.list_projects()

    @app.post("/projects/discover")
    def discover_projects(authorization: str | None = Header(default=None)) -> object:
        _auth(authorization)
        return orchestrator.discover_projects()

    @app.post("/projects/index")
    async def index_projects(
        payload: IndexPayload,
        authorization: str | None = Header(default=None),
    ) -> object:
        _auth(authorization)
        return await orchestrator.index_projects(payload.slugs)

    @app.post("/projects/manual")
    async def add_project_manual(
        payload: ManualAddPayload,
        authorization: str | None = Header(default=None),
    ) -> object:
        _auth(authorization)
        try:
            return await orchestrator.add_project_manual(payload.path)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc))

    @app.get("/projects/{slug}/detail")
    def project_detail(
        slug: str,
        authorization: str | None = Header(default=None),
    ) -> object:
        _auth(authorization)
        try:
            return orchestrator.project_detail(slug)
        except KeyError:
            raise HTTPException(status_code=404, detail=f"Project {slug!r} not found")

    @app.patch("/projects/{slug}")
    def patch_project(
        slug: str,
        payload: RunCommandPayload,
        authorization: str | None = Header(default=None),
    ) -> object:
        _auth(authorization)
        try:
            return orchestrator.update_project_run_command(slug, payload.run_command)
        except KeyError:
            raise HTTPException(status_code=404, detail=f"Project {slug!r} not found")

    @app.delete("/projects/{slug}")
    def delete_project(
        slug: str,
        authorization: str | None = Header(default=None),
    ) -> object:
        _auth(authorization)
        removed = orchestrator.remove_project(slug)
        if not removed:
            raise HTTPException(status_code=404, detail=f"Project {slug!r} not found")
        return {"ok": True}

    @app.post("/projects/{slug}/open")
    def open_project(
        slug: str,
        payload: OpenPayload,
        authorization: str | None = Header(default=None),
    ) -> object:
        _auth(authorization)
        try:
            orchestrator.open_project(slug, payload.target)
        except KeyError:
            raise HTTPException(status_code=404, detail=f"Project {slug!r} not found")
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        except PermissionError as exc:
            raise HTTPException(status_code=403, detail=str(exc))
        return {"ok": True}

    @app.post("/projects/{slug}/start")
    async def start_project(
        slug: str,
        authorization: str | None = Header(default=None),
    ) -> object:
        _auth(authorization)
        try:
            return await orchestrator.start_project(slug)
        except KeyError:
            raise HTTPException(status_code=404, detail=f"Project {slug!r} not found")
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc))

    @app.post("/projects/{slug}/stop")
    async def stop_project(
        slug: str,
        authorization: str | None = Header(default=None),
    ) -> object:
        _auth(authorization)
        try:
            return await orchestrator.stop_project(slug)
        except KeyError:
            raise HTTPException(status_code=404, detail=f"Project {slug!r} not found")

    @app.get("/projects/{slug}/status")
    def project_status(
        slug: str,
        authorization: str | None = Header(default=None),
    ) -> object:
        _auth(authorization)
        try:
            return orchestrator.project_status(slug)
        except KeyError:
            raise HTTPException(status_code=404, detail=f"Project {slug!r} not found")

    # ------------------------------------------------------------------
    # Nexus Home web dashboard (Phase 3W)
    # ------------------------------------------------------------------

    @app.get("/app", response_class=HTMLResponse)
    def nexus_home() -> str:
        # Bearer token is templated into the page: the server binds localhost
        # only and this is a single-user machine — tradeoff recorded in
        # OPEN_QUESTIONS.md ("token-templating tradeoff").
        html = (_WEBAPP_DIR / "index.html").read_text(encoding="utf-8")
        return html.replace("{{NEXUS_TOKEN}}", token)

    app.mount(
        "/app/static",
        StaticFiles(directory=_WEBAPP_DIR / "static"),
        name="webapp-static",
    )

    @app.get("/health")
    async def health() -> object:
        rungs = await orchestrator.cascade.health_check_all()
        result: dict[str, object] = {
            "healthy": any(s.healthy for s in rungs.values()),
            "last_checked": datetime.now(UTC).isoformat(timespec="seconds"),
            "rungs": {pos: s.model_dump(mode="json") for pos, s in rungs.items()},
        }
        pool_status = orchestrator.cascade.pool_health_status()
        if pool_status is not None:
            result["pool"] = pool_status
        return result

    return app


def _load_or_create_token(vault_root: Path) -> str:
    token_path = vault_root / "Orchestrator" / "Config" / "auth.token"
    if token_path.exists():
        return token_path.read_text(encoding="utf-8").strip()
    token_path.parent.mkdir(parents=True, exist_ok=True)
    tok = secrets.token_hex(32)
    token_path.write_text(tok, encoding="utf-8")
    try:
        token_path.chmod(0o600)
    except OSError:
        pass
    return tok
