from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Query
from typing import Annotated
import sqlite3
from fastapi.middleware.cors import CORSMiddleware
from starlette.responses import StreamingResponse
from .realtime import changes
from pydantic import BaseModel, field_validator
from .system_notifications import notifications
from .agent_manager import initialize_persistence, shutdown_agents
from .agent_manager import get_agent_output
from .output_history import OUTPUT_PAGE_SIZE, OUTPUT_MAX_LIMIT

from .agent_manager import AgentType, CodexSandbox, decide_always_agent, disable_always_agent, decide_similar_agent, disable_similar_agent, decide_agent, get_agent, get_agents, redirect_agent, reply_agent, start_agent, stop_agent, stop_branch

@asynccontextmanager
async def lifespan(app):
    initialize_persistence()
    try:
        yield
    finally:
        shutdown_agents()


app = FastAPI(title="AI Agent Control Center", lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173", "http://127.0.0.1:5173"],
    allow_methods=["GET", "POST"],
    allow_headers=["Content-Type"],
)


class NotificationPreference(BaseModel):
    enabled: bool


@app.post("/notifications/preferences")
def set_notification_preference(request: NotificationPreference):
    notifications.set_enabled(request.enabled)
    return {"enabled": request.enabled}


@app.post("/notifications/heartbeat")
def notification_heartbeat():
    notifications.heartbeat()
    return {"status": "ok"}


class StartAgentRequest(BaseModel):
    task: str
    agent_type: AgentType = "mock"
    sandbox: CodexSandbox = "read-only"


class RedirectAgentRequest(BaseModel):
    instruction: str

    @field_validator("instruction")
    @classmethod
    def instruction_not_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("Redirect instruction must not be blank.")
        return value


class ReplyAgentRequest(BaseModel):
    answer: str

    @field_validator("answer")
    @classmethod
    def answer_not_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("Reply answer must not be blank.")
        return value


@app.get("/health")
def health():
    return {"status": "ok"}


@app.post("/agents/start")
def start_mock_agent(request: StartAgentRequest):
    return _start_agent_response(request)


@app.post("/agents/{parent_id}/children/start")
def start_child_agent(parent_id: str, request: StartAgentRequest):
    return _start_agent_response(request, parent_id)


def _start_agent_response(request: StartAgentRequest, parent_id: str | None = None):
    try:
        if parent_id is None:
            agent_id = start_agent(request.task, request.agent_type, request.sandbox)
        else:
            agent_id = start_agent(request.task, request.agent_type, request.sandbox, parent_id=parent_id)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except (OSError, RuntimeError) as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    return {
        "agent_id": agent_id,
        "status": "running",
        "parent_id": parent_id,
        "task": request.task,
        "agent_type": request.agent_type,
        "sandbox": request.sandbox if request.agent_type == "codex" else None,
    }


@app.get("/agents")
def list_agents():
    return {"agents": get_agents()}


@app.get("/agents/{agent_id}")
def get_mock_agent(agent_id: str, include_output: bool = True):
    result = get_agent(agent_id, include_output=include_output)
    if result is None:
        raise HTTPException(status_code=404, detail="Agent not found")
    return result


@app.get("/agents/{agent_id}/output")
def agent_output(agent_id: str,
                 limit: Annotated[int, Query(ge=1, le=OUTPUT_MAX_LIMIT)] = OUTPUT_PAGE_SIZE,
                 after: Annotated[int | None, Query(ge=0)] = None,
                 before: Annotated[int | None, Query(ge=0)] = None):
    try:
        result = get_agent_output(agent_id, limit, after, before)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except (sqlite3.Error, OSError, RuntimeError) as exc:
        raise HTTPException(status_code=503, detail='Output history is temporarily unavailable.') from exc
    if result is None:
        raise HTTPException(status_code=404, detail='Agent not found')
    return result


@app.post("/agents/{agent_id}/stop")
def stop_mock_agent(agent_id: str):
    try:
        result = stop_agent(agent_id)
    except (OSError, RuntimeError) as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    if result is None:
        raise HTTPException(status_code=404, detail="Agent not found")
    return result


@app.post("/agents/{agent_id}/redirect")
def redirect_codex_agent(agent_id: str, request: RedirectAgentRequest):
    try:
        result = redirect_agent(agent_id, request.instruction)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except (OSError, RuntimeError) as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    if result is None:
        raise HTTPException(status_code=404, detail="Agent not found")
    return result


@app.post("/agents/{agent_id}/reply")
def reply_codex_agent(agent_id: str, request: ReplyAgentRequest):
    try:
        result = reply_agent(agent_id, request.answer)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except (OSError, RuntimeError) as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    if result is None:
        raise HTTPException(status_code=404, detail="Agent not found")
    return result


@app.post("/agents/{agent_id}/decide")
def decide_codex_agent(agent_id: str):
    try:
        result = decide_agent(agent_id)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except (OSError, RuntimeError) as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    if result is None:
        raise HTTPException(status_code=404, detail="Agent not found")
    return result


@app.post("/agents/{agent_id}/decide-similar")
def decide_similar_codex_agent(agent_id: str):
    try:
        result = decide_similar_agent(agent_id)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except (OSError, RuntimeError) as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    if result is None:
        raise HTTPException(status_code=404, detail="Agent not found")
    return result


@app.post("/agents/{agent_id}/decide-similar/disable")
def disable_similar_codex_agent(agent_id: str):
    try:
        result = disable_similar_agent(agent_id)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    if result is None:
        raise HTTPException(status_code=404, detail="Agent not found")
    return result


@app.post("/agents/{agent_id}/decide-always")
def decide_always_codex_agent(agent_id: str):
    try:
        result = decide_always_agent(agent_id)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except (OSError, RuntimeError) as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    if result is None:
        raise HTTPException(status_code=404, detail="Agent not found")
    return result


@app.post("/agents/{agent_id}/decide-always/disable")
def disable_always_codex_agent(agent_id: str):
    try:
        result = disable_always_agent(agent_id)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    if result is None:
        raise HTTPException(status_code=404, detail="Agent not found")
    return result


@app.post("/agents/{agent_id}/stop-branch")
def stop_agent_branch(agent_id: str):
    result = stop_branch(agent_id)
    if result is None:
        raise HTTPException(status_code=404, detail="Agent not found")
    return result


@app.get("/events")
async def agent_events():
    return StreamingResponse(changes.stream(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})
