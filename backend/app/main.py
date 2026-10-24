from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Query, Request
import getpass
from typing import Annotated, Literal
import sqlite3
from fastapi.middleware.cors import CORSMiddleware
from fastapi.openapi.utils import get_openapi
from starlette.responses import JSONResponse, StreamingResponse
from . import security
from . import process_supervision
from . import agent_removal, recovery_actions, responsibility, providers, provider_onboarding
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
    manager.start_diagnostics_worker()
    # In-process update awareness only: about once a day while running, never while closed.
    updates = manager.start_update_scheduler()
    try:
        yield
    finally:
        if updates is not None:
            updates.stop()
        provider_onboarding.shutdown()  # Ends and records running setup operations before the database closes.
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
    model: str | None = None
    effort: str | None = None
    attempt_unverified: StrictBool = False
    # Auto allocation for this root Agent's delegated Children: the user's own
    # authorization (allowed providers), stored as the new Task's policy.
    auto_allocation_providers: list[AgentType] | None = None

    @field_validator('model')
    @classmethod
    def valid_model(cls, value):
        return providers.validate_model(value)

    @field_validator('effort')
    @classmethod
    def valid_effort(cls, value):
        return providers.validate_effort(value)

    @field_validator('task')
    @classmethod
    def valid_task(cls, value):
        # The task text becomes the Task description: same rules as POST /tasks.
        return normalize_task_description(value)

    @model_validator(mode='after')
    def eligible_autonomy(self):
        if 'project_id' in self.model_fields_set and (self.project_id is None or not self.project_id.strip()):
            raise ValueError('Select a valid Project or omit project_id for legacy creation.')
        if self.autonomous and not providers.managed_capable(self.agent_type):
            raise ValueError('Autonomous mode is not available for this provider: '
                             + providers.adapter(self.agent_type).capability('autonomous'))
        return self


class PromptConfigFields(BaseModel):
    """A next-prompt override: applies to this one instruction and never changes the Agent's defaults."""
    model_override: StrictBool = False
    model: str | None = None
    effort_override: StrictBool = False
    effort: str | None = None
    confirm_new_session: StrictBool = False
    attempt_unverified: StrictBool = False

    @field_validator('model')
    @classmethod
    def valid_prompt_model(cls, value):
        return providers.validate_model(value)

    @field_validator('effort')
    @classmethod
    def valid_prompt_effort(cls, value):
        return providers.validate_effort(value)

    def prompt_config(self):
        return {'model': self.model, 'model_set': self.model_override, 'effort': self.effort,
                'effort_set': self.effort_override, 'confirm_new_session': self.confirm_new_session,
                'attempt_unverified': self.attempt_unverified}


class RedirectAgentRequest(PromptConfigFields):
    instruction: str

    @field_validator("instruction")
    @classmethod
    def instruction_not_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("Redirect instruction must not be blank.")
        if len(value) > TASK_DESCRIPTION_LIMIT:
            raise ValueError(f"Redirect instruction must contain at most {TASK_DESCRIPTION_LIMIT} characters.")
        return value


class ReplyAgentRequest(PromptConfigFields):
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
        model = {'model': request.model} if request.model is not None else {}
        if request.effort is not None:
            model['effort'] = request.effort
        if request.attempt_unverified:
            model['attempt_unverified'] = True
        if request.auto_allocation_providers is not None and (parent_id is not None or not request.autonomous):
            raise ValueError('Auto allocation applies to delegated Children of a new autonomous root agent.')
        if request.auto_allocation_providers is not None:
            manager.allocation.normalize_policy(True, request.auto_allocation_providers)
        if parent_id is None:
            agent_id = start_agent(request.task, request.agent_type, request.sandbox,
                                   **({'project_id': request.project_id} if request.project_id is not None else {}),
                                   **capabilities, **model)
            if request.auto_allocation_providers is not None:
                task_ids = manager._task_store().agent_task_ids(agent_id)
                if task_ids:
                    # The new root Task has no policy of its own yet: an explicit creation precondition.
                    manager.set_auto_allocation_policy(task_ids[0], enabled=True,
                                                       allowed=request.auto_allocation_providers, expected_revision=None)
        else:
            agent_id = start_agent(request.task, request.agent_type, request.sandbox, parent_id=parent_id, **model)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except providers.ProviderRefused as exc:
        # Actionable: the provider is not usable now (never replaced by another).
        raise HTTPException(status_code=409, detail=str(exc)) from exc
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
        "model": request.model,
        "effort": request.effort,
        "sandbox": request.sandbox if request.agent_type != "mock" else None,
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
    model: str | None = None
    effort: str | None = None
    attempt_unverified: StrictBool = False

    @field_validator('model')
    @classmethod
    def valid_model(cls, value):
        return providers.validate_model(value)

    @field_validator('effort')
    @classmethod
    def valid_effort(cls, value):
        return providers.validate_effort(value)


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


def _execution_action(action):
    try:
        return action()
    except manager.AttestationRefused as exc:
        raise HTTPException(status_code=409, detail={'code': 'attestation_refused', 'message': str(exc),
                                                     'reasons': exc.reasons, 'execution': exc.detail}) from exc
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=409, detail={'code': 'attestation_invalid', 'message': str(exc)}) from exc
    except (sqlite3.Error, OSError, RuntimeError) as exc:
        raise HTTPException(status_code=503, detail='Execution recovery is unavailable. Please retry.') from exc


def _local_actor(request: Request):
    """What this unauthenticated local API can honestly record about who acted: context, not identity."""
    try:
        account = getpass.getuser()
    except Exception:
        account = None
    return {'channel': 'local_api', 'authenticated': False, 'csrf_token_presented': True,
            'origin': request.headers.get('origin'), 'client_host': request.client.host if request.client else None,
            'user_agent': (request.headers.get('user-agent') or '')[:200] or None, 'backend_account': account,
            'note': 'No user authentication exists: this is the request context and the account running the backend, '
                    'not a verified person.'}


class AttestationRequest(BaseModel):
    model_config = ConfigDict(extra='forbid')
    request_key: str
    reviewed_diagnostic_id: str
    statement: str
    reason: str | None = None


@app.get('/executions/unresolved')
def unresolved_executions_route(agent_id: str | None = None, task_id: str | None = None):
    """Read-only: unresolved executions of an Agent (and its Tasks) or of a Task."""
    if not agent_id and not task_id:
        raise HTTPException(status_code=422, detail='agent_id or task_id is required')
    return _execution_action(lambda: manager.unresolved_executions(agent_id=agent_id, task_id=task_id))


@app.get('/executions/{execution_id}')
def execution_route(execution_id: str):
    return _execution_action(lambda: manager.execution_detail(execution_id))


@app.post('/executions/{execution_id}/diagnostics')
def refresh_execution_diagnostics_route(execution_id: str):
    """Explicit refresh: authoritative reconciliation, then fresh observations. Never settles by itself."""
    return _execution_action(lambda: manager.refresh_execution_diagnostics(execution_id))


@app.post('/executions/{execution_id}/attestation')
def attest_execution_route(execution_id: str, body: AttestationRequest, request: Request):
    """Human responsibility attestation; the server re-checks eligibility freshly and may refuse."""
    return _execution_action(lambda: manager.attest_execution(
        execution_id, request_key=body.request_key, reviewed_diagnostic_id=body.reviewed_diagnostic_id,
        statement=body.statement, reason=body.reason, actor=_local_actor(request)))


RemovalMode = Literal['single', 'retire_branch', 'continue_children']
RemovalDisposition = Literal['preserve', 'cancel']


class RemovalPreviewRequest(BaseModel):
    model_config = ConfigDict(extra='forbid')
    mode: RemovalMode = 'single'
    task_disposition: RemovalDisposition = 'preserve'


class RemoveAgentRequest(BaseModel):
    model_config = ConfigDict(extra='forbid')
    request_key: str
    plan_token: str
    mode: RemovalMode = 'single'
    task_disposition: RemovalDisposition = 'preserve'
    reason: str | None = None


class RemovalRetryRequest(BaseModel):
    model_config = ConfigDict(extra='forbid')
    confirm_ended: StrictBool = False


def _removal_action(action):
    """Removal errors carry a stable code; an unsettled operation is reported as 503."""
    try:
        result = action()
    except agent_removal.RemovalError as exc:
        raise HTTPException(status_code=exc.status, detail={'code': exc.code, 'message': str(exc), **exc.details}) from exc
    except recovery_actions.RecoveryRefused as exc:
        raise HTTPException(status_code=409, detail={'code': 'recovery_refused', 'message': str(exc)}) from exc
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except (sqlite3.Error, OSError, RuntimeError) as exc:
        raise HTTPException(status_code=503, detail='Agent removal is unavailable; inspect the removal and retry.') from exc
    if isinstance(result, dict) and result.get('phase') == 'recovery_required':
        raise HTTPException(status_code=503, detail={'code': 'removal_recovery_required',
                                                     'message': result['last_error'] or 'Removal requires attention',
                                                     'removal': result})
    return result


class ResponsibilityTransferRequest(BaseModel):
    model_config = ConfigDict(extra='forbid')
    request_key: str
    expected_owner_assignment_id: str | None = None
    agent_type: AgentType = 'mock'
    sandbox: CodexSandbox | None = None
    reason: str | None = None


def _responsibility_action(action):
    try:
        return action()
    except responsibility.TransferError as exc:
        raise HTTPException(status_code=exc.status, detail={'code': exc.code, 'message': str(exc), **exc.details}) from exc
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=409, detail={'code': 'transfer_refused', 'message': str(exc)}) from exc
    except (sqlite3.Error, OSError, RuntimeError) as exc:
        raise HTTPException(status_code=503, detail='Responsibility transfer is unavailable. Please retry.') from exc


@app.get('/tasks/{task_id}/responsibility')
def task_responsibility_route(task_id: str):
    """Read-only: the Task's current owner, whether it awaits a new one, and why a transfer is refused."""
    return _responsibility_action(lambda: manager.responsibility_state(task_id))


@app.post('/tasks/{task_id}/responsibility/transfer')
def transfer_responsibility_route(task_id: str, request: ResponsibilityTransferRequest):
    """Explicit: a new Agent takes over a Task whose owner was removed (validated, journaled, idempotent)."""
    return _responsibility_action(lambda: manager.transfer_responsibility(
        task_id, request_key=request.request_key, expected_owner_assignment_id=request.expected_owner_assignment_id,
        agent_type=request.agent_type, sandbox=request.sandbox, reason=request.reason))


@app.post('/agents/{agent_id}/removal-preview')
def removal_preview_route(agent_id: str, request: RemovalPreviewRequest | None = None):
    request = request or RemovalPreviewRequest()
    return _removal_action(lambda: manager.removal_preview(agent_id, request.mode, request.task_disposition))


@app.post('/agents/{agent_id}/remove')
def remove_agent_route(agent_id: str, request: RemoveAgentRequest):
    return _removal_action(lambda: manager.remove_agent(
        agent_id, request_key=request.request_key, plan_token=request.plan_token, mode=request.mode,
        task_disposition=request.task_disposition, reason=request.reason))


@app.get('/agents/{agent_id}/removals')
def agent_removals_route(agent_id: str):
    return {'removals': _removal_action(lambda: manager.list_agent_removals(agent_id))}


@app.get('/agent-removals/{removal_id}')
def agent_removal_route(removal_id: str):
    try:
        return manager.get_removal(removal_id)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except (sqlite3.Error, OSError, RuntimeError) as exc:
        raise HTTPException(status_code=503, detail='Agent removal is unavailable. Please retry.') from exc


@app.post('/agent-removals/{removal_id}/retry')
def retry_agent_removal_route(removal_id: str, request: RemovalRetryRequest | None = None):
    request = request or RemovalRetryRequest()
    return _removal_action(lambda: manager.retry_removal(removal_id, request.confirm_ended))


@app.post('/tasks/{task_id}/orchestration/wake', status_code=202)
def wake_task_orchestration_route(task_id: str):
    """Explicit operator wake (for example after a restart); only queues one bounded pass."""
    def wake():
        if manager._task_store().get_task(task_id) is None:
            raise LookupError('Task not found')
        return {'task_id': task_id, 'wake': manager.wake_orchestration(task_id, reason='operator')}
    return _task_action(wake)


class AutoAllocationRequest(BaseModel):
    model_config = ConfigDict(extra='forbid')
    enabled: StrictBool
    # Optimistic concurrency: the own policy's revision the editor loaded (null: none existed).
    expected_revision: int | None = None
    providers: list[AgentType] = []
    models: dict[AgentType, list[str]] = {}
    preferences: list[str] = []
    profile: dict[str, dict[str, list[str]]] = {}


@app.get('/tasks/{task_id}/auto-allocation')
def auto_allocation_route(task_id: str):
    """The effective Auto policy (own or inherited) and the Children's allocation decisions."""
    return _task_action(lambda: manager.auto_allocation_view(task_id))


AUTO_REVISION_REQUIRED = 'expected_revision is required: send the policy revision you loaded, or null only to create a policy for a Task that has none of its own. Reload the policy if you do not have it.'


@app.put('/tasks/{task_id}/auto-allocation')
def set_auto_allocation_route(task_id: str, request: AutoAllocationRequest):
    """The user's explicit Auto authorization for this Task and its subtree.

    Every external write carries an optimistic-concurrency precondition: the revision the
    client loaded, or explicit null to create the Task's first own policy. A request that
    omits it is refused, never applied without the check.
    """
    if 'expected_revision' not in request.model_fields_set:
        raise HTTPException(status_code=428, detail=AUTO_REVISION_REQUIRED)
    return _task_action(lambda: manager.set_auto_allocation_policy(
        task_id, enabled=request.enabled, allowed=request.providers, models=request.models,
        preferences=request.preferences, profile=request.profile, expected_revision=request.expected_revision))


@app.delete('/tasks/{task_id}/auto-allocation')
def clear_auto_allocation_route(task_id: str, expected_revision: int | None = None):
    """Remove this Task's own Auto policy (it then follows an ancestor's policy, if any).
    Requires the revision being removed; a missing or stale one is refused."""
    if expected_revision is None:
        raise HTTPException(status_code=428, detail=AUTO_REVISION_REQUIRED)
    return _task_action(lambda: manager.clear_auto_allocation_policy(task_id, expected_revision=expected_revision))


# ---------------------------------------------------------------- Stage 2G: AI Providers

class ProviderPolicyRequest(BaseModel):
    model_config = ConfigDict(extra='forbid')
    allowed: list[Literal['codex', 'claude', 'gemini']]
    default_provider: AgentType | None = None
    default_model: str | None = None
    default_effort: str | None = None
    expected_revision: int


@app.get('/provider-policy')
def provider_policy_route():
    """Global provider permissions (an empty allowlist means no real provider) and new-root defaults."""
    return _task_action(manager.provider_policy_view)


@app.put('/provider-policy')
def set_provider_policy_route(request: ProviderPolicyRequest):
    """The user's global allowlist and defaults; the revision loaded is required (409 when stale)."""
    return _task_action(lambda: manager.set_provider_policy(
        allowed=request.allowed, default_provider=request.default_provider, default_model=request.default_model,
        default_effort=request.default_effort, expected_revision=request.expected_revision))


class TaskProviderRestrictionRequest(BaseModel):
    model_config = ConfigDict(extra='forbid')
    providers: list[Literal['codex', 'claude', 'gemini']]
    expected_revision: int | None = None


@app.get('/tasks/{task_id}/provider-restrictions')
def task_provider_restrictions_route(task_id: str):
    return _task_action(lambda: manager.task_provider_restrictions(task_id))


@app.put('/tasks/{task_id}/provider-restrictions')
def set_task_provider_restrictions_route(task_id: str, request: TaskProviderRestrictionRequest):
    """Narrow the providers this Task subtree may use (never wider than inherited)."""
    if 'expected_revision' not in request.model_fields_set:
        raise HTTPException(status_code=428, detail=PROVIDER_REVISION_REQUIRED)
    return _task_action(lambda: manager.set_task_provider_restrictions(
        task_id, request.providers, expected_revision=request.expected_revision))


@app.delete('/tasks/{task_id}/provider-restrictions')
def clear_task_provider_restrictions_route(task_id: str, expected_revision: int | None = None):
    if expected_revision is None:
        raise HTTPException(status_code=428, detail=PROVIDER_REVISION_REQUIRED)
    return _task_action(lambda: manager.clear_task_provider_restrictions(task_id, expected_revision=expected_revision))


PROVIDER_REVISION_REQUIRED = ('expected_revision is required: send the restriction revision you loaded, or null only to '
                              'create a restriction for a Task that has none.')


@app.get('/providers/onboarding')
def provider_onboarding_route():
    """Installation, sign-in, enablement, permission, billing, models, usage and updates (no probe)."""
    return _task_action(manager.onboarding_view)


class ProviderOperationPlanRequest(BaseModel):
    model_config = ConfigDict(extra='forbid')
    action: Literal['install', 'update', 'login', 'logout']
    channel: Literal['latest', 'stable', 'preview', 'nightly'] | None = None


@app.post('/providers/{provider}/operations/plan')
def plan_provider_operation_route(provider: str, request: ProviderOperationPlanRequest):
    """The exact operation an approval would authorize; nothing runs."""
    return _task_action(lambda: manager.plan_provider_operation(_known_provider(provider), request.action, request.channel))


class ProviderOperationStartRequest(BaseModel):
    model_config = ConfigDict(extra='forbid')
    plan_id: str
    digest: str
    approved: StrictBool


@app.post('/providers/{provider}/operations', status_code=202)
def start_provider_operation_route(provider: str, request: ProviderOperationStartRequest):
    """Run a reviewed plan. The approval is bound to its identity and digest and is used once."""
    if request.approved is not True:
        raise HTTPException(status_code=422, detail='Explicit approval is required to run a setup operation.')
    return _task_action(lambda: manager.start_provider_operation(_known_provider(provider), request.plan_id, request.digest))


@app.get('/provider-operations/{operation_id}')
def provider_operation_route(operation_id: str):
    return _task_action(lambda: manager.provider_operation(operation_id))


@app.post('/provider-operations/{operation_id}/cancel')
def cancel_provider_operation_route(operation_id: str):
    return _task_action(lambda: manager.cancel_provider_operation(operation_id))


class ProviderOperationResolveRequest(BaseModel):
    model_config = ConfigDict(extra='forbid')
    confirm_ended: StrictBool


@app.post('/provider-operations/{operation_id}/resolve')
def resolve_provider_operation_route(operation_id: str, request: ProviderOperationResolveRequest):
    """Your confirmation that an operation whose end could not be verified has ended (refused while its Job runs)."""
    return _task_action(lambda: manager.resolve_provider_operation(operation_id, request.confirm_ended))


@app.post('/providers/updates/check')
def check_provider_updates_route():
    """Read release metadata now (rate limited); never installs anything."""
    return _task_action(manager.check_provider_updates)


@app.get('/providers/notifications')
def provider_notifications_route():
    return _task_action(manager.provider_notifications)


class ProviderNotificationRequest(BaseModel):
    model_config = ConfigDict(extra='forbid')
    version: str
    mode: Literal['dismiss', 'remind_later']


@app.post('/providers/{provider}/notifications/dismiss')
def dismiss_provider_notification_route(provider: str, request: ProviderNotificationRequest):
    return _task_action(lambda: manager.dismiss_provider_notification(_known_provider(provider), request.version, request.mode))


@app.get('/tasks/{task_id}/orchestration')
def task_orchestration_route(task_id: str):
    return _task_action(lambda: manager.orchestration_status(task_id))


@app.get('/tasks/{task_id}/workspace')
def task_workspace_route(task_id: str):
    return _task_action(lambda: manager.evaluate_workspace_freshness(manager._task_store(), task_id))


@app.post('/tasks/{task_id}/start-agent')
def start_task_agent_route(task_id: str, request: StartTaskAgentRequest):
    options = {'model': request.model} if request.model is not None else {}
    if request.effort is not None:
        options['effort'] = request.effort
    if request.attempt_unverified:
        options['attempt_unverified'] = True
    return _task_action(lambda: manager.start_task_agent(task_id, request.agent_type, request.sandbox, **options))


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
        result = redirect_agent(agent_id, request.instruction, request.prompt_config())
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
        result = reply_agent(agent_id, request.answer, request.prompt_config())
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


class ProviderSettingsRequest(BaseModel):
    model_config = ConfigDict(extra='forbid')
    enabled: StrictBool | None = None
    billing_confirmed: StrictBool | None = None
    models: list[str] | None = None


class ProviderChangeRequest(BaseModel):
    model_config = ConfigDict(extra='forbid')
    provider: AgentType
    model: str | None = None
    sandbox: CodexSandbox | None = None
    effort: str | None = None
    attempt_unverified: StrictBool = False

    @field_validator('effort')
    @classmethod
    def valid_change_effort(cls, value):
        return providers.validate_effort(value)

    @field_validator('model')
    @classmethod
    def valid_model(cls, value):
        return providers.validate_model(value)


def _provider_action(action):
    try:
        return action()
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except providers.ProviderRefused as exc:
        raise HTTPException(status_code=409, detail=exc.as_dict()) from exc
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except (sqlite3.Error, OSError, RuntimeError) as exc:
        raise HTTPException(status_code=503, detail='Provider operation is unavailable. Please retry.') from exc


def _known_provider(provider):
    if provider not in providers.IDS:
        raise LookupError('Unknown provider')
    return provider


@app.get('/providers')
def provider_list():
    """Read-only: support, settings, usage and the last readiness check (never probes)."""
    return _provider_action(lambda: {'providers': manager.provider_statuses(check=False)})


@app.post('/providers/check')
def provider_check_all():
    """Explicit, non-billable local checks (installation, version, sign-in, billing route)."""
    return _provider_action(lambda: {'providers': manager.provider_statuses(check=True)})


@app.post('/providers/{provider}/check')
def provider_check(provider: str):
    return _provider_action(lambda: manager.check_provider(_known_provider(provider)))


@app.put('/providers/{provider}/settings')
def provider_settings(provider: str, request: ProviderSettingsRequest):
    return _provider_action(lambda: manager.update_provider_settings(
        _known_provider(provider), enabled=request.enabled, billing_confirmed=request.billing_confirmed,
        models=request.models))


@app.post('/providers/{provider}/usage/allow')
def provider_allow_work(provider: str):
    """The user's explicit decision that a usage limit renewed; no paid request checks it."""
    return _provider_action(lambda: manager.allow_provider_work(_known_provider(provider)))


@app.post('/agents/{agent_id}/provider')
def change_agent_provider_route(agent_id: str, request: ProviderChangeRequest):
    """Provider, model or narrower access change at a safe boundary; nothing is launched."""
    result = _provider_action(lambda: manager.change_agent_provider(
        agent_id, provider=request.provider, model=request.model, sandbox=request.sandbox, effort=request.effort,
        attempt_unverified=request.attempt_unverified))
    if result is None:
        raise HTTPException(status_code=404, detail='Agent not found')
    return result


class AgentDefaultsRequest(BaseModel):
    model_config = ConfigDict(extra='forbid')
    model_set: StrictBool = False
    model: str | None = None
    effort_set: StrictBool = False
    effort: str | None = None
    attempt_unverified: StrictBool = False

    @field_validator('model')
    @classmethod
    def valid_default_model(cls, value):
        return providers.validate_model(value)

    @field_validator('effort')
    @classmethod
    def valid_default_effort(cls, value):
        return providers.validate_effort(value)


@app.post('/agents/{agent_id}/defaults')
def agent_defaults_route(agent_id: str, request: AgentDefaultsRequest):
    """Change one Agent's own default model and/or effort (applies from its next instruction)."""
    result = _provider_action(lambda: manager.update_agent_defaults(
        agent_id, model=request.model, effort=request.effort, model_set=request.model_set,
        effort_set=request.effort_set, attempt_unverified=request.attempt_unverified))
    if result is None:
        raise HTTPException(status_code=404, detail='Agent not found')
    return result


@app.get('/providers/{provider}/models')
def provider_models(provider: str):
    """The cached model catalog with status and provenance (never runs discovery)."""
    return _provider_action(lambda: manager.model_catalog_view(_known_provider(provider)))


@app.post('/providers/{provider}/models/refresh')
def provider_models_refresh(provider: str, stale_only: bool = False):
    """Non-billable model discovery (coalesced and rate limited). Never a model request."""
    return _provider_action(lambda: manager.model_catalog_view(
        _known_provider(provider), refresh=True, trigger='panel_stale' if stale_only else 'user'))


@app.post('/providers/{provider}/usage/refresh')
def provider_refresh_usage(provider: str):
    """A supported, non-billable usage check (Claude Code /usage). Evidence only: it resumes nothing."""
    return _provider_action(lambda: manager.refresh_provider_usage(_known_provider(provider)))


class ResumeRequest(PromptConfigFields):
    model_config = ConfigDict(extra='forbid')
    explicit_retry: StrictBool = False


@app.post('/agents/{agent_id}/resume')
def resume_agent_route(agent_id: str, request: ResumeRequest | None = None):
    """The user's explicit Resume of one waiting Agent after every readiness, policy and admission check."""
    explicit = bool(request and request.explicit_retry)
    config = request.prompt_config() if request else None
    result = _provider_action(lambda: manager.continue_agent(agent_id, explicit_retry=explicit, config=config))
    if result is None:
        raise HTTPException(status_code=404, detail='Agent not found')
    return result


@app.post('/agents/{agent_id}/continue')
def continue_agent_route(agent_id: str):
    """Explicitly continue a waiting Agent after a provider wait or a provider change."""
    result = _provider_action(lambda: manager.continue_agent(agent_id))
    if result is None:
        raise HTTPException(status_code=404, detail='Agent not found')
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
