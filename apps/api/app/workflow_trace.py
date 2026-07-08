from __future__ import annotations

import time
from contextlib import contextmanager
from contextvars import ContextVar, Token
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Iterator
from uuid import uuid4


@dataclass
class WorkflowTraceState:
    id: str
    run_id: str
    level: str
    selected_steps: set[str] | None
    started_at: datetime
    started_monotonic: float
    steps: list[dict[str, Any]] = field(default_factory=list)


_current_trace: ContextVar[WorkflowTraceState | None] = ContextVar("workflow_trace", default=None)


def internal_monitoring_requested(value: str | None) -> bool:
    return (value or "").strip().lower() in {"internal", "workflow", "deep"}


def parse_trace_step_header(value: str | None) -> set[str] | None:
    raw_steps = [part.strip() for part in (value or "").split(",") if part.strip()]
    if not raw_steps or any(step.lower() == "all" for step in raw_steps):
        return None
    return {step for step in raw_steps}


def start_workflow_trace(
    enabled: bool,
    selected_steps: set[str] | None = None,
    run_id: str | None = None,
    level: str = "internal",
) -> Token[WorkflowTraceState | None]:
    state = (
        WorkflowTraceState(
            id=str(uuid4()),
            run_id=run_id or "",
            level=level or "internal",
            selected_steps=selected_steps,
            started_at=datetime.now(timezone.utc),
            started_monotonic=time.perf_counter(),
        )
        if enabled
        else None
    )
    return _current_trace.set(state)


def reset_workflow_trace(token: Token[WorkflowTraceState | None]) -> None:
    _current_trace.reset(token)


def finish_workflow_trace() -> dict[str, Any] | None:
    state = _current_trace.get()
    if not state:
        return None

    ended_at = datetime.now(timezone.utc)
    duration_ms = (time.perf_counter() - state.started_monotonic) * 1000
    return {
        "id": state.id,
        "run_id": state.run_id,
        "level": state.level,
        "duration_ms": round(duration_ms, 3),
        "started_at": state.started_at.isoformat(),
        "ended_at": ended_at.isoformat(),
        "steps": state.steps,
    }


@contextmanager
def trace_step(name: str, attributes: dict[str, Any] | None = None) -> Iterator[None]:
    state = _current_trace.get()
    if not state or not step_selected(state, name):
        yield
        return

    started_at = datetime.now(timezone.utc)
    started_monotonic = time.perf_counter()
    status = "ok"
    error: str | None = None
    try:
        yield
    except Exception as exc:
        status = "error"
        error = str(exc)
        raise
    finally:
        ended_at = datetime.now(timezone.utc)
        duration_ms = (time.perf_counter() - started_monotonic) * 1000
        state.steps.append(
            {
                "id": str(uuid4()),
                "name": name,
                "status": status,
                "duration_ms": round(duration_ms, 3),
                "started_at": started_at.isoformat(),
                "ended_at": ended_at.isoformat(),
                "attributes": safe_attributes(attributes or {}),
                "error": error,
            }
        )


def record_trace_event(name: str, attributes: dict[str, Any] | None = None) -> None:
    """Append an instantaneous event (e.g. LLM token usage) to the active trace."""
    state = _current_trace.get()
    if not state or not step_selected(state, name):
        return

    now = datetime.now(timezone.utc)
    state.steps.append(
        {
            "id": str(uuid4()),
            "name": name,
            "status": "ok",
            "duration_ms": 0.0,
            "started_at": now.isoformat(),
            "ended_at": now.isoformat(),
            "attributes": safe_attributes(attributes or {}),
            "error": None,
        }
    )


def step_selected(state: WorkflowTraceState, name: str) -> bool:
    return state.selected_steps is None or name in state.selected_steps


def safe_attributes(attributes: dict[str, Any]) -> dict[str, Any]:
    safe: dict[str, Any] = {}
    for key, value in attributes.items():
        if isinstance(value, (str, int, float, bool)) or value is None:
            safe[key] = value
        else:
            safe[key] = str(value)
    return safe
