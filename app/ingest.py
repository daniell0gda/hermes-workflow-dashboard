"""Turns a publish payload into normalised rows.

The worker sends what it already produces (status.json, events.json,
graph.json, metrics.json and the raw markdown). Everything the views need to be
fast and readable - durations, revision count, verdict, token spend - is derived
here once, at write time.
"""

from __future__ import annotations

import json
import re
from typing import Any, Mapping, Sequence

from . import formatting
from .config import ISSUE_NUMBER_PLACEHOLDER, ISSUE_PROJECT_PLACEHOLDER, IssueLinking
from .repository import RUN_STATUSES, Repository

MAX_RUN_ID_LENGTH = 128

PATCHABLE_FIELDS = ("status", "phase", "active_node", "last_node", "error", "ended_at")

MAX_GH_STATUS_LENGTH = 32

# Forge states that mean the issue is resolved. Everything else - open,
# reopened, a project column name - is work still outstanding.
GH_DONE_STATUSES = frozenset({"closed", "merged"})

KIND_PRIMARY = "primary"
KIND_CLUSTER = "cluster"
KIND_CODER = "coder"

# Display order and labels for the documents the worker always produces.
PRIMARY_DOCUMENTS: dict[str, str] = {
    "team-leader.report": "Team-leader report",
    "report": "Detailed report",
    "manual-report": "Manual test report",
    "request": "Request",
    "plan": "Plan",
    "status": "Acceptance status",
    "check": "Check",
    "code": "Implementation",
    "revisions": "Revisions",
}

# Aliases seen in metrics.json for the same figure.
COST_KEYS = ("cost_usd", "total_cost_usd", "cost")
INPUT_KEYS = ("input_tokens", "total_input_tokens", "prompt_tokens")
OUTPUT_KEYS = ("output_tokens", "total_output_tokens", "completion_tokens")
METRIC_SCOPES = ("totals", "usage", "tokens")

# The team-leader report states the verdict; check.md is the fallback for runs
# that never reached the gate. Observed values: pass, fixable, blocked,
# unknown, design_failure.
_TEAM_LEADER_VERDICT = re.compile(
    r"^\s*[-*]\s*\*\*Classification:\*\*\s*([A-Za-z_-]+)", re.MULTILINE | re.IGNORECASE
)
_CHECK_VERDICT = re.compile(r"^\s*classification:\s*([A-Za-z_-]+)", re.MULTILINE | re.IGNORECASE)

# Any forge's issue URL: GitHub and Gitea use <repo>/issues/<n>, GitLab
# <repo>/-/issues/<n>. Group 1 is the repository, group 2 the issue number.
_ISSUE_URL = re.compile(
    r"https?://[^\s)\]<>\"'`]+?/([A-Za-z0-9._-]+)/(?:-/)?issues/(\d+)\b", re.IGNORECASE
)

# The request document opens with e.g. "# Request: #116 game-ready-blocks (r6)"
# or "# Request: issue #110 — ...".
_REQUEST_ISSUE_NUMBER = re.compile(r"^#\s*Request:.*?#(\d{1,5})\b", re.MULTILINE | re.IGNORECASE)

MAX_ISSUE_URL_LENGTH = 500

_UNSAFE_ID = re.compile(r"[^A-Za-z0-9._-]")


class IngestError(ValueError):
    """The payload cannot be stored as given."""


def sanitize_run_id(value: Any) -> str:
    cleaned = _UNSAFE_ID.sub("", str(value or ""))
    if not cleaned or cleaned in {".", ".."} or len(cleaned) > MAX_RUN_ID_LENGTH:
        raise IngestError(f"invalid run id: {value!r}")

    return cleaned


def sanitize_slug(value: Any) -> str:
    """Strip traversal while keeping the ``clusters/foo`` shape."""
    text = str(value or "").replace("\\", "/")
    text = re.sub(r"\.md$", "", text, flags=re.IGNORECASE)

    segments = []
    for segment in text.split("/"):
        cleaned = _UNSAFE_ID.sub("", segment)
        if cleaned and cleaned not in {".", ".."}:
            segments.append(cleaned)

    return "/".join(segments[:2])[:191]


def publish(
    repository: Repository,
    run_id: str,
    payload: Mapping[str, Any],
    issue_linking: IssueLinking | None = None,
) -> dict[str, Any]:
    status = payload.get("status") if isinstance(payload.get("status"), dict) else {}
    raw_events = payload.get("events") if isinstance(payload.get("events"), list) else []
    raw_documents = payload.get("documents") if isinstance(payload.get("documents"), list) else []

    metrics = payload.get("metrics") if isinstance(payload.get("metrics"), dict) else {}
    if not metrics and isinstance(status.get("metrics"), dict):
        metrics = status["metrics"]

    # Normalise in memory first: the derived run fields are computed from the
    # events and documents, and the run row has to exist before its children
    # can reference it.
    events = _event_rows(raw_events)
    documents = _document_rows(raw_documents)

    fields = _run_fields(status, events, documents, metrics, payload)
    fields.update(_issue(payload, documents, issue_linking or IssueLinking()))

    repository.save_run(run_id, fields)
    repository.replace_events(run_id, events)
    repository.replace_documents(run_id, documents)

    return {"run_id": run_id, "events": len(events), "documents": len(documents)}


def patch(repository: Repository, run_id: str, changes: Mapping[str, Any]) -> dict[str, Any]:
    existing = repository.find_run(run_id)
    if existing is None:
        raise LookupError(run_id)

    fields: dict[str, Any] = {"heartbeat_at": formatting.now_text()}
    for key in PATCHABLE_FIELDS:
        if key in changes:
            fields[key] = _patched_value(key, changes[key])

    status = fields.get("status", existing["status"])
    if status != "running" and not fields.get("ended_at") and not existing["ended_at"]:
        fields["ended_at"] = fields["heartbeat_at"]

    # A patch never moves started_at; only a full publish sets it.
    duration = _elapsed_ms(existing["started_at"], fields.get("ended_at") or existing["ended_at"])
    if duration is not None:
        fields["duration_ms"] = duration

    repository.save_run(run_id, fields)

    return repository.find_run(run_id) or existing


def set_issue_state(
    repository: Repository, run_id: str, changes: Mapping[str, Any]
) -> dict[str, Any]:
    """Record the forge's state for the issue this run came from.

    ``done`` is derived rather than accepted, so the flag can never contradict
    the state it summarises. The heartbeat is deliberately left alone: polling
    the forge says nothing about whether the worker is still alive.
    """
    existing = repository.find_run(run_id)
    if existing is None:
        raise LookupError(run_id)

    gh_status = _text(changes.get("gh_status"), MAX_GH_STATUS_LENGTH)
    if not gh_status:
        raise IngestError("gh_status is required, e.g. 'open' or 'closed'")

    repository.save_run(
        run_id,
        {"gh_status": gh_status, "done": int(gh_status.lower() in GH_DONE_STATUSES)},
    )

    return repository.find_run(run_id) or existing


def _event_rows(events: Sequence[Any]) -> list[dict[str, Any]]:
    rows = []
    for index, event in enumerate(events):
        if not isinstance(event, dict):
            continue
        rows.append(
            {
                "seq": _integer(event.get("sequence"), default=index + 1),
                "node": _text(event.get("node"), 64),
                "status": _text(event.get("status"), 32),
                "duration_ms": _integer(event.get("duration_ms")),
                "occurred_at": formatting.to_storage(event.get("timestamp")),
                "summary": _text(event.get("summary"), 200_000),
                "error": _text(event.get("error"), 20_000),
            }
        )

    return rows


def _document_rows(documents: Sequence[Any]) -> list[dict[str, Any]]:
    rows: dict[str, dict[str, Any]] = {}
    order = list(PRIMARY_DOCUMENTS)

    for document in documents:
        if not isinstance(document, dict) or "body" not in document:
            continue

        slug = sanitize_slug(document.get("slug"))
        body = str(document["body"])
        if not slug or not body.strip():
            continue

        kind = _kind_for(slug, document.get("kind"))
        rows[slug] = {
            "slug": slug,
            "title": (str(document.get("title") or "").strip() or _title_for(slug))[:191],
            "kind": kind,
            "sort_order": order.index(slug) if slug in order else (400 if kind == KIND_PRIMARY else 500),
            "body": body,
        }

    return list(rows.values())


def _kind_for(slug: str, given: Any) -> str:
    if slug.startswith("clusters/"):
        return KIND_CLUSTER
    if slug.startswith("coder-reports/"):
        return KIND_CODER
    if given in (KIND_CLUSTER, KIND_CODER):
        return str(given)

    return KIND_PRIMARY


def _title_for(slug: str) -> str:
    if slug in PRIMARY_DOCUMENTS:
        return PRIMARY_DOCUMENTS[slug]

    leaf = slug.rsplit("/", 1)[-1]
    leaf = re.sub(r"^\d+[-_]", "", leaf)

    return leaf.replace("-", " ").replace("_", " ").replace(".", " ").capitalize()


def _run_fields(
    status: Mapping[str, Any],
    events: Sequence[Mapping[str, Any]],
    documents: Sequence[Mapping[str, Any]],
    metrics: Mapping[str, Any],
    payload: Mapping[str, Any],
) -> dict[str, Any]:
    started = formatting.to_storage(status.get("started_at"))
    ended = formatting.to_storage(status.get("ended_at"))
    state = _text(status.get("status"), 32) or "unknown"
    usage = _usage(metrics)
    graph = payload.get("graph")

    return {
        "feature": _text(status.get("feature"), 191),
        "status": state if state in RUN_STATUSES else "unknown",
        "classification": _classification(documents),
        "phase": _text(status.get("phase"), 64),
        "active_node": _text(status.get("active_node"), 64),
        "last_node": _text(status.get("last_node"), 64) or _last_event_node(events),
        "host": _text(status.get("host"), 191),
        "started_at": started,
        "ended_at": ended,
        "heartbeat_at": formatting.to_storage(status.get("heartbeat_at")) or formatting.now_text(),
        "duration_ms": _elapsed_ms(started, ended),
        "worker_ms": _worker_ms(events),
        "event_count": len(events),
        "revisions": _revisions(events),
        "cost_usd": usage["cost_usd"],
        "input_tokens": usage["input_tokens"],
        "output_tokens": usage["output_tokens"],
        "error": _text(status.get("error"), 20_000),
        "graph": json.dumps(graph) if isinstance(graph, dict) and graph else None,
        "metrics": json.dumps(metrics) if metrics else None,
    }


def _revisions(events: Sequence[Mapping[str, Any]]) -> int:
    """A revision is a second or later pass through the coding stage."""
    code_passes = sum(1 for event in events if event.get("node") == "code")

    return max(0, code_passes - 1)


def _worker_ms(events: Sequence[Mapping[str, Any]]) -> int | None:
    values = [event["duration_ms"] for event in events if event.get("duration_ms") is not None]

    return sum(int(value) for value in values) if values else None


def _last_event_node(events: Sequence[Mapping[str, Any]]) -> str | None:
    for event in reversed(list(events)):
        if event.get("node"):
            return str(event["node"])

    return None


def _issue(
    payload: Mapping[str, Any],
    documents: Sequence[Mapping[str, Any]],
    linking: IssueLinking,
) -> dict[str, Any]:
    """Resolve the issue this run came from, and the project it belongs to.

    Best evidence first:

    1. What the worker states outright in the payload.
    2. A real issue URL inside the run's own documents - the request document
       carries one on most runs. This is authoritative and needs no
       configuration, whichever project the run belongs to, because the URL
       names its own repository.
    3. The number stated in the request heading, turned into a URL only from a
       configured template. Without one the number is still shown, unlinked,
       rather than guessed at.

    Never inferred: the issue number from the run id (ids also embed timestamps
    and revision counters), and the project from workspace paths (those name
    the local runner workspace, which is measurably not the repository - runs
    saying `godot-td` belong to the `poke-defense-godot` repo).
    """
    bodies = {document["slug"]: document["body"] or "" for document in documents}
    stated_project = _project_key(payload.get("project"))
    stated_number = _integer(payload.get("issue_number"))

    url = _safe_url(payload.get("issue_url"))
    if not url:
        request_first = ["request", *(slug for slug in bodies if slug != "request")]
        for slug in request_first:
            match = _ISSUE_URL.search(bodies.get(slug, ""))
            if match:
                url = match.group(0)[:MAX_ISSUE_URL_LENGTH]
                break

    if url:
        match = _ISSUE_URL.search(url)
        return {
            "issue_url": url,
            "issue_number": stated_number or (int(match.group(2)) if match else None),
            "project": stated_project or (_project_key(match.group(1)) if match else None),
        }

    heading = _REQUEST_ISSUE_NUMBER.search(bodies.get("request", ""))
    number = stated_number or (int(heading.group(1)) if heading else None)
    if number is None:
        return {"issue_url": None, "issue_number": None, "project": stated_project}

    return {
        "issue_url": _build_issue_url(linking.template_for(stated_project), stated_project, number),
        "issue_number": number,
        "project": stated_project,
    }


def _build_issue_url(template: str | None, project: str | None, number: int) -> str | None:
    """Fill a template. A {project} placeholder with no known project yields no
    link at all, which beats linking to the wrong repository."""
    if not template:
        return None

    if ISSUE_PROJECT_PLACEHOLDER in template:
        if not project:
            return None
        template = template.replace(ISSUE_PROJECT_PLACEHOLDER, project)

    return _safe_url(template.replace(ISSUE_NUMBER_PLACEHOLDER, str(number)))


def _project_key(value: Any) -> str | None:
    """A repository name, tolerating a stray trailing dot or slash."""
    text = _text(value, 100)
    if not text:
        return None

    cleaned = _UNSAFE_ID.sub("", text.strip("./ ").split("/")[-1]).strip(".")

    return cleaned or None


def _safe_url(value: Any) -> str | None:
    """Only http(s) URLs are stored; these end up in an href."""
    text = _text(value, MAX_ISSUE_URL_LENGTH)
    if not text or not text.lower().startswith(("http://", "https://")):
        return None

    return text


def _classification(documents: Sequence[Mapping[str, Any]]) -> str | None:
    bodies = {document["slug"]: document["body"] for document in documents}

    match = _TEAM_LEADER_VERDICT.search(bodies.get("team-leader.report", ""))
    if match:
        return match.group(1).lower()

    match = _CHECK_VERDICT.search(bodies.get("check", ""))

    return match.group(1).lower() if match else None


def _usage(metrics: Mapping[str, Any]) -> dict[str, float | None]:
    """Token and cost totals.

    The worker reports usage as a per-invocation list rather than as totals, so
    the list is summed. A cost carrying ``cost_status: unknown`` is a
    placeholder zero - the model's pricing was never resolved - and is reported
    as "no data" instead of a misleading $0.00.
    """
    usage = {
        "cost_usd": _first_number(metrics, COST_KEYS),
        "input_tokens": _first_number(metrics, INPUT_KEYS),
        "output_tokens": _first_number(metrics, OUTPUT_KEYS),
    }

    invocations = metrics.get("invocations")
    if not isinstance(invocations, list) or not invocations:
        return usage

    summed = _sum_invocations(invocations)

    return {key: summed[key] if value is None else value for key, value in usage.items()}


def _sum_invocations(invocations: Sequence[Any]) -> dict[str, float | None]:
    cost = 0.0
    tokens = {"input_tokens": 0.0, "output_tokens": 0.0}
    cost_known = False
    tokens_known = False

    for invocation in invocations:
        if not isinstance(invocation, dict):
            continue

        for key in tokens:
            value = invocation.get(key)
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                tokens[key] += float(value)
                tokens_known = True

        value = invocation.get("cost_usd")
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            cost += float(value)
            if invocation.get("cost_status", "unknown") != "unknown":
                cost_known = True

    return {
        "cost_usd": cost if cost_known else None,
        "input_tokens": tokens["input_tokens"] if tokens_known else None,
        "output_tokens": tokens["output_tokens"] if tokens_known else None,
    }


def _first_number(metrics: Mapping[str, Any], keys: Sequence[str]) -> float | None:
    scopes = [metrics]
    scopes.extend(metrics[scope] for scope in METRIC_SCOPES if isinstance(metrics.get(scope), dict))

    for scope in scopes:
        for key in keys:
            value = scope.get(key)
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                return float(value)

    return None


def _elapsed_ms(started: str | None, ended: str | None) -> int | None:
    start = formatting.parse_any(started)
    end = formatting.parse_any(ended)
    if start is None or end is None or end < start:
        return None

    return int((end - start).total_seconds() * 1000)


def _patched_value(key: str, value: Any) -> Any:
    if key == "ended_at":
        return formatting.to_storage(value)
    if key == "status":
        status = _text(value, 32)
        return status if status in RUN_STATUSES else "unknown"

    return _text(value, 20_000 if key == "error" else 64)


def _text(value: Any, length: int) -> str | None:
    if value is None or isinstance(value, (dict, list, bool)):
        return None

    text = str(value).strip()

    return text[:length] if text else None


def _integer(value: Any, default: int | None = None) -> int | None:
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        return default
    try:
        return max(0, int(float(value)))
    except (TypeError, ValueError):
        return default
