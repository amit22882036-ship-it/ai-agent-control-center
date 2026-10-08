"""Task identity and validation, independent of worker naming and processes."""
from typing import Literal, get_args

TaskStatus = Literal['pending', 'in_progress', 'waiting', 'blocked', 'paused', 'completed', 'canceled']
AssignmentEndReason = Literal['completed', 'stopped', 'reassigned', 'paused', 'blocked', 'resource_conflict', 'resource_deadlock', 'external_resource_unavailable', 'canceled']
TASK_TITLE_LIMIT = 100
TASK_DESCRIPTION_LIMIT = 20000


def normalize_task_title(value):
    normalized = ' '.join(value.split())
    if not normalized or len(normalized) > TASK_TITLE_LIMIT:
        raise ValueError(f'Task title must contain 1–{TASK_TITLE_LIMIT} characters')
    return normalized


def normalize_task_description(value):
    normalized = value.strip()
    if not normalized or len(normalized) > TASK_DESCRIPTION_LIMIT:
        raise ValueError(f'Task description must contain 1–{TASK_DESCRIPTION_LIMIT} characters')
    return normalized


def task_title(description):
    for line in description.splitlines():
        normalized = ' '.join(line.split())
        if normalized:
            return normalized[:TASK_TITLE_LIMIT]
    return 'Untitled task'


def validate_task_status(status):
    if status not in get_args(TaskStatus):
        raise ValueError('Invalid task status')
    return status


def validate_end_reason(reason):
    if reason not in get_args(AssignmentEndReason):
        raise ValueError('Invalid assignment end reason')
    return reason
