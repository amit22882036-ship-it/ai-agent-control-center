from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Query
from typing import Annotated, Literal
import sqlite3
from fastapi.middleware.cors import CORSMiddleware
from fastapi.openapi.utils import get_openapi
from starlette.responses import JSONResponse, StreamingResponse
from . import security
from . import process_supervision
from .realtime import changes
from pydantic import BaseModel, ConfigDict, StrictBool, field_validator, model_validator
from .system_notifications import notifications
from .agent_manager import initialize_persistence, shutdown_agents
from .agent_manager import get_agent_output
from .output_history import OUTPUT_PAGE_SIZE, OUTPUT_MAX_LIMIT
from . import agent_manager as manager
from .task_domain import TASK_DESCRIPTION_LIMIT, normalize_task_title, normalize_task_description
from .project_domain import project_name

from .agent_manager import rename_agent, set_agent_color, get_name_history
from .agent_names import DisplayColor
from .agent_names import default_display_name, validate_display_name
from .agent_manager import AgentType, CodexSandbox, decide_always_agent, disable_always_agent, decide_similar_agent, disable_similar_agent, decide_agent, get_agent, get_agents, redirect_agent, reply_agent, start_agent, stop_agent, stop_branch

@asynccontextmanager
async def lifespan(app):
    # Harmless supervised probe; each execution is still verified individually.
    process_supervision.self_test()
    initialize_persistence()
    manager.start_recovery_sweep()
    try:
        yield
    finally:
        shutdown_agents()


app = FastAPI(title="AI Agent Control Center", lifespan=lifespan)
# In memory only and new per backend process; never persisted or logged.
app.state.csrf_token = security.new_csrf_token()
# Added innermost first: HostGuard -> CORS -> RequestGuard -> application.
app.add_middleware(security.RequestGuard, token=app.state.csrf_token)
app.add_middleware(
    CORSMiddleware,
    allow_origins=list(security.allowed_origins()),
    allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE"],
    allow_headers=["Content-Type", security.CSRF_HEADER],
)
app.add_middleware(security.HostGuard)


@app.get(security.TOKEN_PATH)
def csrf_token():
    return JSONResponse({"csrf_token": app.state.csrf_token},
                        headers={"Cache-Control": "no-store"})


def _openapi():
    """Document the CSRF header so /docs can send it after Authorize."""
    if app.openapi_schema is None:
        schema = get_openapi(title=app.title, version=app.version, routes=app.routes)
        schema.setdefault("components", {})["securitySchemes"] = {
            "csrfToken": {"type": "apiKey", "in": "header", "name": security.CSRF_HEADER,
                          "description": f"Value from GET {security.TOKEN_PATH}"}}
        for operations in schema.get("paths", {}).values():
            for method, operation in operations.items():
                if method.upper() not in security.SAFE_METHODS:
                    operation["security"] = [{"csrfToken": []}]
        app.openapi_schema = schema
    return app.openapi_schema


app.openapi = _openapi


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
    model_config = ConfigDict(extra='forbid')
    task: str
    agent_type: AgentType = "mock"
    sandbox: CodexSandbox = "read-only"
    autonomous: StrictBool = False
    project_id: str | None = None

    @field_validator('task')
    @classmethod
    def valid_task(cls, value):
        # The task text becomes the Task description: same rules as POST /tasks.
        return normalize_task_description(value)

    @model_validator(mode='after')
    def eligible_autonomy(self):
        if 'project_id' in self.model_fields_set and (self.project_id is None or not self.project_id.strip()):
            raise ValueError('Select a valid Project or omit project_id for legacy creation.')
        if self.autonomous and self.agent_type != 'codex':
            raise ValueError('Autonomous mode requires Codex.')
        return self


class RedirectAgentRequest(BaseModel):
    instruction: str

    @field_validator("instruction")
    @classmethod
    def instruction_not_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("Redirect instruction must not be blank.")
        if len(value) > TASK_DESCRIPTION_LIMIT:
            raise ValueError(f"Redirect instruction must contain at most {TASK_DESCRIPTION_LIMIT} characters.")
        return value


class ReplyAgentRequest(BaseModel):
    answer: str

    @field_validator("answer")
    @classmethod
    def answer_not_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("Reply answer must not be blank.")
        if len(value) > TASK_DESCRIPTION_LIMIT:
            raise ValueError(f"Reply answer must contain at most {TASK_DESCRIPTION_LIMIT} characters.")
        return value


@app.get("/health")
def health():
    return {"status": "ok"}


@app.get("/system/process-supervision")
def process_supervision_status():
    """supervised | unavailable (with reason) | not_applicable; read-only, never probes."""
    return process_supervision.status()


@app.post("/agents/start")
def start_mock_agent(request: StartAgentRequest):
    return _start_agent_response(request)


@app.post("/agents/{parent_id}/children/start")
def start_child_agent(parent_id: str, request: StartAgentRequest):
    return _start_agent_response(request, parent_id)


def _start_agent_response(request: StartAgentRequest, parent_id: str | None = None):
    try:
        if request.project_id is not None and parent_id is not None:
            raise ValueError('Child agents inherit their parent Task Project; do not supply project_id.')
        if request.autonomous and parent_id is not None:
            raise ValueError('Autonomous mode is available only when creating a new root agent.')
        capabilities = {key: True for key in (
            'planning_enabled', 'delegation_protocol_enabled', 'child_materialization_enabled',
            'hybrid_delegation_enabled', 'recursive_delegation_enabled')} if request.autonomous else {}
        if parent_id is None:
            agent_id = start_agent(request.task, request.agent_type, request.sandbox,
                                   **({'project_id': request.project_id} if request.project_id is not None else {}), **capabilities)
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


@app.get('/workspace')
def workspace_view(limit: int = Query(500, ge=1, le=500), after: int = Query(0, ge=0)):
    from .presentation import workspace
    return _task_action(lambda: workspace(manager._task_store(), limit=limit, after=after))


@app.get('/agents/{agent_id}/activity')
def agent_activity(agent_id: str):
    from .presentation import activity
    return _task_action(lambda: activity(manager._task_store(), agent_id))


@app.get('/agents/{agent_id}/hierarchy')
def agent_hierarchy(agent_id: str):
    from .presentation import hierarchy
    return _task_action(lambda: hierarchy(manager._task_store(), agent_id))


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
    model_config = ConfigDict(extra='forbid')
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
    def register():
        from .workspace_git import repository
        repository(request.root_path)
        return manager._task_store().create_project(request.name, request.root_path)
    return _project_action(register)


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


class RecoveryConfirmation(BaseModel):
    model_config = ConfigDict(extra='forbid')
    confirm_ended: StrictBool


@app.post('/tasks/{task_id}/process-ownership/confirm-stopped')
def confirm_process_ownership_route(task_id: str, request: RecoveryConfirmation):
    return _task_action(lambda: manager.confirm_process_ownership_stopped(task_id, request.confirm_ended))


@app.post('/delegations/{delegation_id}/recovery/resolve-launch')
def resolve_launch_route(delegation_id: str, request: RecoveryConfirmation):
    return _task_action(lambda: manager.resolve_launch(delegation_id, request.confirm_ended))


@app.post('/tasks/{task_id}/recovery/rearm')
def rearm_continuation_route(task_id: str, request: RecoveryConfirmation):
    return _task_action(lambda: manager.rearm_continuation(task_id, request.confirm_ended))


@app.post('/tasks/{task_id}/recovery/reverify')
def reverify_transitions_route(task_id: str, request: RecoveryConfirmation):
    return _task_action(lambda: manager.reverify_transitions(task_id, request.confirm_ended))


@app.post('/tasks/{task_id}/orchestration/wake', status_code=202)
def wake_task_orchestration_route(task_id: str):
    """Explicit operator wake (for example after a restart); only queues one bounded pass."""
    def wake():
        if manager._task_store().get_task(task_id) is None:
            raise LookupError('Task not found')
        return {'task_id': task_id, 'wake': manager.wake_orchestration(task_id, reason='operator')}
    return _task_action(wake)


@app.get('/tasks/{task_id}/orchestration')
def task_orchestration_route(task_id: str):
    return _task_action(lambda: manager.orchestration_status(task_id))


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


@app.post("/agents/{agent_id}/recovery/handoff")
def recover_lost_watcher_route(agent_id: str):
    """Explicit recovery of an execution whose watcher was lost; runs its finalization once."""
    try:
        result = manager.recover_lost_watcher(agent_id)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except (OSError, RuntimeError, sqlite3.Error) as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
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
