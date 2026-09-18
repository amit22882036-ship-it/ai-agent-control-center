from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, field_validator
from .system_notifications import notifications

from .agent_manager import AgentType, CodexSandbox, decide_agent, get_agent, get_agents, redirect_agent, reply_agent, start_agent, stop_agent

app = FastAPI(title="AI Agent Control Center")
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
    try:
        agent_id = start_agent(request.task, request.agent_type, request.sandbox)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except (OSError, RuntimeError) as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    return {
        "agent_id": agent_id,
        "status": "running",
        "task": request.task,
        "agent_type": request.agent_type,
        "sandbox": request.sandbox if request.agent_type == "codex" else None,
    }


@app.get("/agents")
def list_agents():
    return {"agents": get_agents()}


@app.get("/agents/{agent_id}")
def get_mock_agent(agent_id: str):
    result = get_agent(agent_id)
    if result is None:
        raise HTTPException(status_code=404, detail="Agent not found")
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
