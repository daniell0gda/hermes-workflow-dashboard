"""Derives the stage-by-stage view of a run.

The worker names the same stage two ways: graph.json uses gerunds
("implementing") while events.json and status.active_node use the worker name
("code"). Both fold onto one canonical key, so the pipeline shape from the graph
can be filled in with real event data.
"""

from __future__ import annotations

import json
import re
from typing import Any, Mapping, Sequence

DONE = "done"
ACTIVE = "active"
FAILED = "failed"
PENDING = "pending"
SKIPPED = "skipped"

# Worker or graph name (lowercased) -> canonical stage key.
ALIASES = {
    "start": "start",
    "starting": "start",
    "init": "start",
    "plan": "plan",
    "planning": "plan",
    "planner": "plan",
    "code": "code",
    "coder": "code",
    "implement": "code",
    "implementing": "code",
    "check": "check",
    "checker": "check",
    "checking": "check",
    "review": "review",
    "reviewer": "review",
    "reviewing": "review",
    "manual-tester": "manual",
    "manual-testing": "manual",
    "team-leader": "report",
    "teamleader": "report",
    "report": "report",
    "reporting": "report",
}

LABELS = {
    "start": "Start",
    "plan": "Plan",
    "code": "Implement",
    "check": "Check",
    "review": "Review",
    "manual": "Manual test",
    "report": "Report",
}

FAILURE_STATES = {"failed", "error", "errored", "timeout"}

_UNSAFE = re.compile(r"[^a-z0-9_-]")


def canonical(name: Any) -> str:
    key = str(name or "").strip().lower()
    if key in ALIASES:
        return ALIASES[key]

    return _UNSAFE.sub("", key)


def _pretty(key: str) -> str:
    if key in LABELS:
        return LABELS[key]

    return key.replace("-", " ").replace("_", " ").capitalize()


def stages(run: Mapping[str, Any], events: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    grouped = _group(events)
    skeleton = _skeleton(run, grouped)
    active = canonical(run.get("active_node") or run.get("phase"))
    running = run.get("status") == "running"
    since = _active_since(run, events)

    result = []
    for key, text in skeleton.items():
        stage_events = grouped.get(key, [])
        state = _state(key, stage_events, active, running, run)
        result.append(
            {
                "key": key,
                "label": text,
                "state": state,
                "pass_states": _pass_states(stage_events, state),
                "passes": len(stage_events),
                "duration_ms": _total_duration(stage_events),
                "since": since if state == ACTIVE else None,
            }
        )

    return result


def progress(stage_list: Sequence[Mapping[str, Any]]) -> int:
    """Fraction of the pipeline that has reached a final state."""
    if not stage_list:
        return 0

    settled = sum(1 for stage in stage_list if stage["state"] in (DONE, FAILED))

    return round(settled / len(stage_list) * 100)


def _group(events: Sequence[Mapping[str, Any]]) -> dict[str, list[Mapping[str, Any]]]:
    grouped: dict[str, list[Mapping[str, Any]]] = {}
    for event in events:
        key = canonical(event.get("node"))
        if key:
            grouped.setdefault(key, []).append(event)

    return grouped


def _skeleton(run: Mapping[str, Any], grouped: Mapping[str, list]) -> dict[str, str]:
    """Stage order: the graph's shape when usable, else the observed order."""
    skeleton = _from_graph(run.get("graph"))

    for key in grouped:
        skeleton.setdefault(key, _pretty(key))

    active = canonical(run.get("active_node") or run.get("phase"))
    if active:
        skeleton.setdefault(active, _pretty(active))

    return skeleton


def _from_graph(graph_json: Any) -> dict[str, str]:
    if not graph_json:
        return {}

    try:
        graph = json.loads(graph_json) if isinstance(graph_json, str) else graph_json
    except (TypeError, ValueError):
        return {}

    nodes = graph.get("nodes") if isinstance(graph, dict) else None
    if not isinstance(nodes, list):
        return {}

    skeleton: dict[str, str] = {}
    for node in nodes:
        identifier = node.get("id") if isinstance(node, dict) else node
        key = canonical(identifier)
        if key and key not in skeleton:
            skeleton[key] = _pretty(key)

    return skeleton


def _state(
    key: str,
    stage_events: Sequence[Mapping[str, Any]],
    active: str,
    running: bool,
    run: Mapping[str, Any],
) -> str:
    """A stage stands where its latest pass left it.

    A pass that failed and was retried is history, not the verdict: a stage
    running again reads as active, and one whose retry succeeded reads as done.
    The individual verdicts survive in ``pass_states``.
    """
    if running and key == active:
        return ACTIVE

    if stage_events:
        return _pass_state(stage_events[-1])

    # The start stage leaves no event behind; the run beginning is the proof.
    if key == "start" and run.get("started_at"):
        return DONE

    return PENDING if running else SKIPPED


def _pass_state(event: Mapping[str, Any]) -> str:
    failed = str(event.get("status") or "").lower() in FAILURE_STATES

    return FAILED if failed else DONE


def _pass_states(stage_events: Sequence[Mapping[str, Any]], state: str) -> list[str]:
    """One verdict per pass, oldest first.

    The pass now running has not reported an event yet, so it is added by hand.
    """
    history = [_pass_state(event) for event in stage_events]
    if state == ACTIVE:
        history.append(ACTIVE)

    return history


def _active_since(run: Mapping[str, Any], events: Sequence[Mapping[str, Any]]) -> str | None:
    """When the pass now running started.

    An event's timestamp is the moment its worker call finished, so the latest
    one dates the start of whatever the run moved on to. Until the first call
    lands, the run's own start is the best answer there is.
    """
    stamps = [event["occurred_at"] for event in events if event.get("occurred_at")]

    return max(stamps) if stamps else run.get("started_at")


def _total_duration(stage_events: Sequence[Mapping[str, Any]]) -> int | None:
    values = [event["duration_ms"] for event in stage_events if event.get("duration_ms") is not None]

    return sum(int(value) for value in values) if values else None
