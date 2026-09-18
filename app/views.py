"""HTML views. Everything rendered here comes out of the database."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Callable, Sequence

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse

from . import formatting, ingest, media, workflow
from .config import Settings
from .dependencies import get_repository, get_settings
from .repository import Repository

router = APIRouter()

RUNS_PER_PAGE = 50
ACTIVE_CARD_LIMIT = 8

DOCUMENT_GROUPS = (
    (ingest.KIND_CLUSTER, "Work clusters"),
    (ingest.KIND_CODER, "Coder reports"),
)


def _render(request: Request, template: str, context: dict[str, Any], status: int = 200) -> HTMLResponse:
    templates = request.app.state.templates
    response = templates.TemplateResponse(request, template, context, status_code=status)
    # Operational data: current, and not for search engines.
    response.headers["Cache-Control"] = "no-store"
    response.headers["X-Robots-Tag"] = "noindex, nofollow"

    return response


def _with_urls(settings: Settings, artifacts: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
    prefix = settings.root_path

    return [
        {
            **artifact,
            "url": prefix + media.url_for(artifact["run_id"], artifact["rel_path"]),
            "thumb_url": prefix + media.thumb_url_for(artifact),
        }
        for artifact in artifacts
    ]


def _media_resolver(artifacts: Sequence[dict[str, Any]]) -> Callable[[str], str | None]:
    """Resolve a markdown image reference against this run's own artifacts.

    Built from an in-memory map so a report with a dozen images costs no extra
    queries. Both the full relative path and the bare filename are accepted,
    because reports reference them either way.
    """
    urls: dict[str, str] = {}
    for artifact in artifacts:
        urls[artifact["rel_path"]] = artifact["url"]
        urls.setdefault(artifact["file_name"], artifact["url"])

    def resolve(reference: str) -> str | None:
        path = media.sanitize_path(reference)
        if not path:
            return None

        return urls.get(path) or urls.get(Path(path).name)

    return resolve


@router.get("/", response_class=HTMLResponse)
def runs_list(
    request: Request,
    status: str = "",
    q: str = "",
    project: str = "",
    done: str = "",
    pg: int = 1,
    repository: Repository = Depends(get_repository),
) -> HTMLResponse:
    result = repository.query_runs(
        status=status, search=q, project=project, done=done, page=pg, per_page=RUNS_PER_PAGE
    )

    active = []
    for run in repository.active_runs(ACTIVE_CARD_LIMIT):
        stages = workflow.stages(run, repository.events_for(run["run_id"]))
        active.append({"run": run, "stages": stages})

    return _render(
        request,
        "list.html",
        {
            "filters": {"status": status, "q": q, "project": project, "done": done},
            "result": result,
            "summary": repository.summary(),
            "active": active,
            # Only worth showing once runs come from more than one project.
            "projects": repository.projects(),
        },
    )


@router.get("/run/{run_id}", response_class=HTMLResponse)
def run_detail(
    request: Request,
    run_id: str,
    repository: Repository = Depends(get_repository),
    settings: Settings = Depends(get_settings),
) -> HTMLResponse:
    try:
        identifier = ingest.sanitize_run_id(run_id)
    except ingest.IngestError:
        return _render(request, "not_found.html", {"run_id": run_id}, status=404)

    run = repository.find_run(identifier)
    if run is None:
        return _render(request, "not_found.html", {"run_id": identifier}, status=404)

    events = repository.events_for(identifier)
    stages = workflow.stages(run, events)
    documents = repository.documents_for(identifier)
    artifacts = _with_urls(settings, repository.artifacts_for(identifier))

    grouped: dict[str, list[dict[str, Any]]] = {
        ingest.KIND_PRIMARY: [],
        ingest.KIND_CLUSTER: [],
        ingest.KIND_CODER: [],
    }
    for document in documents:
        grouped.setdefault(document["kind"], []).append(document)

    invocations = _invocations(run["metrics"])

    sections = {"overview": "Overview", "workflow": "Workflow", "timeline": "Timeline"}
    if invocations:
        sections["usage"] = "Usage"
    if artifacts:
        sections["screenshots"] = "Screenshots"
    if documents:
        sections["reports"] = "Reports"

    return _render(
        request,
        "run.html",
        {
            "run": run,
            "events": events,
            "stages": stages,
            "documents": grouped,
            "document_groups": DOCUMENT_GROUPS,
            "media": artifacts,
            "resolve_media": _media_resolver(artifacts),
            "invocations": invocations,
            "sections": sections,
        },
    )


def _invocations(metrics_json: Any) -> list[dict[str, Any]]:
    if not metrics_json:
        return []

    try:
        metrics = json.loads(metrics_json)
    except (TypeError, ValueError):
        return []

    invocations = metrics.get("invocations") if isinstance(metrics, dict) else None
    if not isinstance(invocations, list):
        return []

    return [item for item in invocations if isinstance(item, dict)]
