"""VoxAero HTTP API and zero-build preview."""

from contextlib import asynccontextmanager
from dataclasses import asdict

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from openai import OpenAI

from backend.config import AIRSPACE_LAYERS, FRONTEND_DIR, LLMSettings, STUDY_BOUNDS
from backend.core.agent import AgentError, DispatchAgent
from backend.core.airspace import Airspace
from backend.schemas import DispatchRequest, DispatchResult, ReplanRequest, ReplanResult


def create_app(settings: LLMSettings | None = None, *, llm_client: OpenAI | None = None) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI):
        airspace = Airspace.from_directory()
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
        }

    @app.post("/api/agent/dispatch")
    def dispatch(payload: DispatchRequest, request: Request) -> DispatchResult:
        return request.app.state.agent.dispatch(payload.query)

    @app.post("/api/agent/replan")
    def replan(payload: ReplanRequest, request: Request) -> ReplanResult:
        return request.app.state.agent.replan_emergency(payload)

    app.mount("/", StaticFiles(directory=FRONTEND_DIR, html=True), name="frontend")
    return app


app = create_app()
