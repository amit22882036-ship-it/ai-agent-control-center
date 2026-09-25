"""Deterministic local names; identity and original task remain independent."""
from typing import Literal, get_args

DisplayColor = Literal["neutral", "violet", "blue", "cyan", "green", "yellow", "orange", "red", "pink"]


def validate_display_color(value: str) -> str:
    if value not in get_args(DisplayColor):
        raise ValueError("Unsupported agent display color.")
    return value

def default_display_name(task: str) -> str:
    words = " ".join(task.split())
    if not words:
        return "Untitled agent"
    return words if len(words) <= 56 else words[:53].rstrip() + "..."


def validate_display_name(value: str) -> str:
    value = " ".join(value.split())
    if not value or len(value) > 80:
        raise ValueError("Agent name must contain between 1 and 80 characters.")
    return value
