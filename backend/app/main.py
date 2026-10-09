from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Query
from typing import Annotated, Literal
import sqlite3
from fastapi.middleware.cors import CORSMiddleware
from starlette.responses import StreamingResponse
from .realtime import changes
from pydantic import BaseModel, field_validator
from .system_notifications import notifications
from .agent_manager import initialize_persistence, shutdown_agents
from .agent_manager import get_agent_output
from .output_history import OUTPUT_PAGE_SIZE, OUTPUT_MAX_LIMIT
from . import agent_manager as manager
from .task_domain import normalize_task_title, normalize_task_description
from .project_domain import project_name

from .agent_manager import rename_agent, set_agent_color, get_name_history
from .agent_names import DisplayColor
from .agent_names import default_display_name, validate_display_name
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
    except (OSError, RuntimeError, sqlite3.Error) as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    return {
        "agent_id": agent_id,
        "status": "running",
        "parent_id": parent_id,
        "task": request.task,
        "display_name": default_display_name(request.task),
        "display_color": "neutral",
        "agent_type": request.agent_type,
        "sandbox": request.sandbox if request.agent_type == "codex" else None,
    }


@app.get("/agents")
def list_agents():
    return {"agents": get_agents()}


@app.get("/agents/{agent_id}")
def get_mock_agent(agent_id: str, include_output: bool = True):
    try:
        result = get_agent(agent_id, include_output=include_output)
    except (sqlite3.Error, OSError) as exc:
        raise HTTPException(status_code=503, detail='Output history is temporarily unavailable.') from exc
    if result is None:
        raise HTTPException(status_code=404, detail="Agent not found")
    return result


class CreateTaskRequest(BaseModel):
    title: str
    description: str
    project_id: str | None = None

    @field_validator('title')
    @classmethod
    def valid_title(cls, value):
        return normalize_task_title(value)

    @field_validator('description')
    @classmethod
    def valid_description(cls, value):
        return normalize_task_description(value)


class StartTaskAgentRequest(BaseModel):
    agent_type: AgentType = 'mock'
    sandbox: CodexSandbox = 'read-only'


def _task_action(action):
    try:
        return action()
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except (sqlite3.Error, OSError, RuntimeError) as exc:
        raise HTTPException(status_code=503, detail='Task operation is unavailable. Please retry.') from exc


@app.post('/tasks')
def create_task_route(request: CreateTaskRequest):
    return _task_action(lambda: manager.create_task(request.title, request.description, request.project_id))


class CreateProjectRequest(BaseModel):
    name: str
    root_path: str

    @field_validator('name')
    @classmethod
    def valid_name(cls, value):
        return project_name(value)


def _project_action(action):
    try:
        return action()
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except (sqlite3.Error, OSError, RuntimeError) as exc:
        raise HTTPException(status_code=503, detail='Project operation is unavailable. Please retry.') from exc


@app.post('/projects')
def create_project_route(request: CreateProjectRequest):
    return _project_action(lambda: manager._task_store().create_project(request.name, request.root_path))


@app.get('/projects')
def list_projects_route():
    return {'projects': _project_action(lambda: manager._task_store().list_projects())}


@app.get('/projects/{project_id}')
def get_project_route(project_id: str):
    result = _project_action(lambda: manager._task_store().get_project(project_id))
    if result is None:
        raise HTTPException(status_code=404, detail='Project not found')
    return result


@app.get('/tasks')
def list_tasks_route():
    return {'tasks': _task_action(manager.get_tasks)}


@app.get('/tasks/{task_id}')
def get_task_route(task_id: str):
    result = _task_action(lambda: manager.get_tasks(task_id))
    if not result:
        raise HTTPException(status_code=404, detail='Task not found')
    return result[0]


@app.get('/tasks/{task_id}/assignments')
def task_assignments_route(task_id: str):
    """Assignment history, oldest first (insertion order resolves timestamp ties)."""
    return {'assignments': _task_action(lambda: manager.get_task_assignments(task_id))}


@app.get('/tasks/{task_id}/workspace')
def task_workspace_route(task_id: str):
    return _task_action(lambda: manager.evaluate_workspace_freshness(manager._task_store(), task_id))


@app.post('/tasks/{task_id}/start-agent')
def start_task_agent_route(task_id: str, request: StartTaskAgentRequest):
    return _task_action(lambda: manager.start_task_agent(task_id, request.agent_type, request.sandbox))


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


class RenameAgentRequest(BaseModel):
    display_name: str

    @field_validator("display_name")
    @classmethod
    def valid_name(cls, value: str) -> str:
        return validate_display_name(value)


@app.post("/agents/{agent_id}/rename")
def rename_agent_route(agent_id: str, request: RenameAgentRequest):
    try:
        result = rename_agent(agent_id, request.display_name)
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    if result is None:
        raise HTTPException(status_code=404, detail="Agent not found")
    return result


class AgentColorRequest(BaseModel):
    display_color: DisplayColor


@app.post("/agents/{agent_id}/color")
def agent_color_route(agent_id: str, request: AgentColorRequest):
    try:
        result = set_agent_color(agent_id, request.display_color)
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    if result is None:
        raise HTTPException(status_code=404, detail="Agent not found")
    return result


@app.get("/agents/{agent_id}/name-history")
def agent_name_history_route(agent_id: str):
    try:
        result = get_name_history(agent_id)
    except (sqlite3.Error, OSError, RuntimeError) as exc:
        raise HTTPException(status_code=503, detail="Name history is temporarily unavailable.") from exc
    if result is None:
        raise HTTPException(status_code=404, detail="Agent not found")
    return result


class IntegrationRequest(BaseModel):
    model_config = {'extra': 'forbid'}


@app.post('/tasks/{task_id}/integrate')
def integrate_task_route(task_id: str, request: IntegrationRequest | None = None):
    result = _task_action(lambda: manager.integrate_task(task_id))
    if result['status'] not in ('applied', 'noop'):
        raise HTTPException(status_code=409, detail=result)
    return result


@app.get('/tasks/{task_id}/integrations')
def task_integrations_route(task_id: str):
    from .integrations import public
    def read():
        store = manager._task_store()
        if store.get_task(task_id) is None:
            raise LookupError('Task not found')
        return {'integrations': [public(r) for r in store.list_integrations(task_id)]}
    return _task_action(read)


@app.get('/integrations/{integration_id}')
def integration_route(integration_id: str):
    from .integrations import public
    def read():
        record = manager._task_store().get_integration(integration_id)
        if record is None:
            raise LookupError('Integration not found')
        return public(record)
    return _task_action(read)


@app.post('/tasks/{task_id}/pause')
def pause_task_route(task_id: str):
    return _task_action(lambda: manager.control_task(task_id, 'paused'))


@app.post('/tasks/{task_id}/resume')
def resume_task_route(task_id: str):
    return _task_action(lambda: manager.control_task(task_id, 'active'))


@app.post('/tasks/{task_id}/cancel')
def cancel_task_route(task_id: str):
    return _task_action(lambda: manager.control_task(task_id, 'canceled'))


class DependencyRequest(BaseModel):
    depends_on_task_id: str


class ControlImpactRequest(BaseModel):
    action: Literal['pause', 'cancel']


@app.post('/tasks/{task_id}/dependencies')
def add_dependency_route(task_id: str, request: DependencyRequest):
    return _task_action(lambda: manager.change_dependency(task_id, request.depends_on_task_id))


@app.delete('/tasks/{task_id}/dependencies/{depends_on_task_id}')
def remove_dependency_route(task_id: str, depends_on_task_id: str):
    return _task_action(lambda: manager.change_dependency(task_id, depends_on_task_id, remove=True))


@app.get('/tasks/{task_id}/dependencies')
def dependencies_route(task_id: str):
    return {'dependencies': _task_action(lambda: manager._task_store().list_dependencies(task_id))}


@app.get('/tasks/{task_id}/dependents')
def dependents_route(task_id: str):
    return {'dependents': _task_action(lambda: manager._task_store().list_dependencies(task_id, reverse=True))}


@app.post('/tasks/{task_id}/control-impact')
def control_impact_route(task_id: str, request: ControlImpactRequest):
    return _task_action(lambda: manager._task_store().control_impact(task_id, request.action))


@app.get('/tasks/{task_id}/control-operations')
def control_operations_route(task_id: str):
    return {'operations': _task_action(lambda: manager._task_store().control_operations(task_id))}


class ResourceClaimRequest(BaseModel):
    resource_type: Literal['file_path', 'port', 'database', 'docker_resource', 'generic']
    resource_key: str
    mode: Literal['advisory', 'shared', 'exclusive'] | None = None
    lifetime: Literal['task', 'worker'] = 'task'
    scope: Literal['project', 'global'] | None = None
    recursive: bool = False


@app.post('/tasks/{task_id}/resource-claims')
def create_resource_claim_route(task_id: str, request: ResourceClaimRequest):
    return _task_action(lambda: manager.create_resource_claim(task_id, **request.model_dump()))


@app.get('/tasks/{task_id}/deadlocks')
def task_deadlocks_route(task_id: str):
    return {'deadlocks': _task_action(lambda: manager._task_store().resource_deadlocks(task_id=task_id))}


@app.get('/resource-deadlocks/{deadlock_id}')
def resource_deadlock_route(deadlock_id: str):
    return _task_action(lambda: manager._task_store().resource_deadlocks(deadlock_id=deadlock_id))


@app.get('/tasks/{task_id}/resource-claims')
def resource_claims_route(task_id: str):
    return {'claims': _task_action(lambda: manager._task_store().resource_claims(task_id))}


@app.delete('/tasks/{task_id}/resource-claims/{claim_id}')
def release_resource_claim_route(task_id: str, claim_id: str):
    return _task_action(lambda: manager.release_resource_claim(task_id, claim_id))


class WorkIntentRequest(BaseModel):
    namespace: str
    key: str
    mode: Literal['advisory', 'single_owner'] = 'advisory'
    delegated_from_intent_id: str | None = None


@app.post('/tasks/{task_id}/work-intents')
def create_work_intent_route(task_id: str, request: WorkIntentRequest):
    return _task_action(lambda: manager.create_work_intent(task_id, **request.model_dump()))


@app.get('/tasks/{task_id}/work-intents')
def work_intents_route(task_id: str):
    return {'intents': _task_action(lambda: manager._task_store().work_intents(task_id))}


@app.delete('/tasks/{task_id}/work-intents/{intent_id}')
def release_work_intent_route(task_id: str, intent_id: str):
    return _task_action(lambda: manager.release_work_intent(task_id, intent_id))


@app.get('/tasks/{task_id}/work-overlaps')
def work_overlaps_route(task_id: str):
    return {'overlaps': _task_action(lambda: manager._task_store().work_intents(task_id, overlaps=True))}


@app.get('/tasks/{task_id}/delegations')
def delegations_route(task_id: str):
    return {'delegations': _task_action(lambda: manager._task_store().list_delegations(task_id))}


@app.get('/delegations/{delegation_id}')
def delegation_route(delegation_id: str):
    return _task_action(lambda: manager._task_store().get_delegation(delegation_id))
