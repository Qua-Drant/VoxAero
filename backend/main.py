"""VoxAero HTTP API and zero-build preview."""

from contextlib import asynccontextmanager
from dataclasses import asdict
from os import getenv
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from openai import OpenAI

from backend.config import AIRSPACE_LAYERS, DATA_DIR, FRONTEND_DIR, LLMSettings, STUDY_BOUNDS
from backend.core.agent import AgentError, DispatchAgent
from backend.core.airspace import Airspace
from backend.schemas import DispatchNotice, DispatchRequest, DispatchResult, ReplanRequest, ReplanResult


def create_app(
    settings: LLMSettings | None = None, *, llm_client: OpenAI | None = None, directory: Path | None = None
) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI):
        scene_directory = directory if directory is not None else Path(getenv("VOXAERO_SCENE_DIR") or DATA_DIR)
        airspace = Airspace.from_directory(scene_directory)
        app.state.agent = DispatchAgent(airspace, settings or LLMSettings.from_environment(), llm_client)
        yield

    app = FastAPI(title="VoxAero API", version="1.0.0", lifespan=lifespan)

    @app.exception_handler(AgentError)
    async def handle_agent_error(request: Request, exc: AgentError) -> JSONResponse:
        return JSONResponse(
            status_code=exc.status,
            content={
                "detail": str(exc),
                "thought_steps": [step.model_dump(mode="json") for step in exc.steps],
            },
        )

    @app.get("/api/health")
    def health(request: Request) -> dict[str, str]:
        return {"service": "VoxAero", "status": "ok", "llm_mode": request.app.state.agent.settings.mode}

    @app.get("/api/layers/all")
    def layers(request: Request) -> dict:
        return {
            **request.app.state.agent.airspace.geojson,
            "airspace_layers": [asdict(layer) for layer in AIRSPACE_LAYERS],
            "bounds": STUDY_BOUNDS,
            "scene_version": request.app.state.agent.airspace.scene_version,
        }

    def current_version(request: Request, expected: str | None) -> str:
        version = request.app.state.agent.airspace.scene_version
        if expected is not None and expected != version:
            raise HTTPException(status_code=409, detail="空域场景已更新，请刷新页面后重新调度。")
        return version

    @app.post("/api/agent/dispatch")
    def dispatch(payload: DispatchRequest, request: Request) -> DispatchResult | DispatchNotice:
        version = current_version(request, payload.scene_version)
        result = request.app.state.agent.dispatch(payload.query, payload.clarification_token)
        result = result.model_copy(update={"scene_version": version})
        if isinstance(result, DispatchNotice) and result.status in {"needs_clarification", "constraint_conflict"}:
            status = 422 if result.status == "needs_clarification" else 409
            return JSONResponse(status_code=status, content=result.model_dump(mode="json"))
        return result

    @app.post("/api/agent/replan")
    def replan(payload: ReplanRequest, request: Request) -> ReplanResult:
        version = current_version(request, payload.scene_version)
        return request.app.state.agent.replan_emergency(payload).model_copy(update={"scene_version": version})

    app.mount("/", StaticFiles(directory=FRONTEND_DIR, html=True), name="frontend")
    return app


app = create_app()
