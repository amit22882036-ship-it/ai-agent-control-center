from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

from .agent_manager import AgentType, get_agent, get_agents, start_agent, stop_agent

app = FastAPI(title="AI Agent Control Center")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173", "http://127.0.0.1:5173"],
    allow_methods=["GET", "POST"],
    allow_headers=["Content-Type"],
)


class StartAgentRequest(BaseModel):
    task: str
    agent_type: AgentType = "mock"


@app.get("/health")
def health():
    return {"status": "ok"}


@app.post("/agents/start")
def start_mock_agent(request: StartAgentRequest):
    try:
        agent_id = start_agent(request.task, request.agent_type)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except (OSError, RuntimeError) as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    return {
        "agent_id": agent_id,
        "status": "running",
        "task": request.task,
        "agent_type": request.agent_type,
    }


@app.get("/agents")
def list_agents():
    return {"agents": get_agents()}


@app.get("/agents/{agent_id}")
def get_mock_agent(agent_id: int):
    result = get_agent(agent_id)
    if result is None:
        raise HTTPException(status_code=404, detail="Agent not found")
    return result


@app.post("/agents/{agent_id}/stop")
def stop_mock_agent(agent_id: int):
    try:
        result = stop_agent(agent_id)
    except (OSError, RuntimeError) as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    if result is None:
        raise HTTPException(status_code=404, detail="Agent not found")
    return result
