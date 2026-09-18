"""JSON API.

Writes require the ``X-API-Key`` header; reads are open. Media is uploaded one
file per request as a raw binary body, which keeps a publish well inside normal
body-size limits and avoids base64 inflating every screenshot by a third.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Body, Depends, HTTPException, Query, Request, Response
from starlette.concurrency import run_in_threadpool

from . import formatting, ingest, media, workflow
from .config import Settings
from .dependencies import get_repository, get_settings, require_api_key
from .repository import PER_PAGE_DEFAULT, Repository

router = APIRouter(prefix="/api")


def _run_id(value: str) -> str:
    try:
        return ingest.sanitize_run_id(value)
    except ingest.IngestError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error


def _shape_run(run: dict[str, Any]) -> dict[str, Any]:
    return {
        "run_id": run["run_id"],
        "feature": run["feature"],
        "status": run["status"],
        "display_status": formatting.display_status(run),
        "health": formatting.health(run),
        "classification": run["classification"],
        "project": run["project"],
        "issue_url": run["issue_url"],
        "issue_number": run["issue_number"],
        "gh_status": run["gh_status"],
        "done": bool(run["done"]),
        "phase": run["phase"],
        "active_node": run["active_node"],
        "last_node": run["last_node"],
        "host": run["host"],
        "error": run["error"],
        "started_at": formatting.iso(run["started_at"]),
        "ended_at": formatting.iso(run["ended_at"]),
        "heartbeat_at": formatting.iso(run["heartbeat_at"]),
        "updated_at": formatting.iso(run["updated_at"]),
        "duration_ms": run["duration_ms"],
        "worker_ms": run["worker_ms"],
        "event_count": run["event_count"],
        "revisions": run["revisions"],
        "cost_usd": run["cost_usd"],
        "input_tokens": run["input_tokens"],
        "output_tokens": run["output_tokens"],
        "url": f"/run/{run['run_id']}",
    }


def _shape_event(event: dict[str, Any]) -> dict[str, Any]:
    return {
        "sequence": event["seq"],
        "node": event["node"],
        "status": event["status"],
        "duration_ms": event["duration_ms"],
        "occurred_at": formatting.iso(event["occurred_at"]),
        "summary": event["summary"],
        "error": event["error"],
    }


def _shape_artifact(artifact: dict[str, Any]) -> dict[str, Any]:
    return {
        "path": artifact["rel_path"],
        "mime": artifact["mime"],
        "bytes": artifact["bytes"],
        "width": artifact["width"],
        "height": artifact["height"],
        "animated": bool(artifact["animated"]),
        "checksum": artifact["checksum"],
        "url": media.url_for(artifact["run_id"], artifact["rel_path"]),
        "thumb_url": media.thumb_url_for(artifact),
    }


# ------------------------------------------------------------------- writes


@router.post("/runs", dependencies=[Depends(require_api_key)])
def publish_run(
    payload: dict[str, Any] = Body(...),
    repository: Repository = Depends(get_repository),
    settings: Settings = Depends(get_settings),
) -> dict[str, Any]:
    run_id = _run_id(payload.get("run_id") or (payload.get("status") or {}).get("run_id", ""))

    with repository.transaction():
        result = ingest.publish(repository, run_id, payload, settings.issue_linking)

    return {"ok": True, **result}


@router.post("/runs/{run_id}/status", dependencies=[Depends(require_api_key)])
def patch_status(
    run_id: str,
    payload: dict[str, Any] = Body(default_factory=dict),
    repository: Repository = Depends(get_repository),
) -> dict[str, Any]:
    identifier = _run_id(run_id)
    try:
        with repository.transaction():
            run = ingest.patch(repository, identifier, payload)
    except LookupError as error:
        raise HTTPException(status_code=404, detail=f"Unknown run id: {identifier}") from error

    return {"ok": True, "run_id": identifier, "status": run["status"], "phase": run["phase"]}


@router.post("/runs/{run_id}/issue", dependencies=[Depends(require_api_key)])
def patch_issue_state(
    run_id: str,
    payload: dict[str, Any] = Body(default_factory=dict),
    repository: Repository = Depends(get_repository),
) -> dict[str, Any]:
    """Record how the run's issue stands on the forge. ``done`` is derived."""
    identifier = _run_id(run_id)
    try:
        with repository.transaction():
            run = ingest.set_issue_state(repository, identifier, payload)
    except LookupError as error:
        raise HTTPException(status_code=404, detail=f"Unknown run id: {identifier}") from error
    except ingest.IngestError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error

    return {
        "ok": True,
        "run_id": identifier,
        "gh_status": run["gh_status"],
        "done": bool(run["done"]),
    }


@router.post("/runs/{run_id}/heartbeat", dependencies=[Depends(require_api_key)])
def heartbeat(
    run_id: str,
    repository: Repository = Depends(get_repository),
) -> dict[str, Any]:
    identifier = _run_id(run_id)
    if not repository.touch_heartbeat(identifier):
        raise HTTPException(status_code=404, detail=f"Unknown run id: {identifier}")

    return {"ok": True, "run_id": identifier}


@router.post("/runs/{run_id}/media", dependencies=[Depends(require_api_key)])
async def upload_media(
    run_id: str,
    request: Request,
    path: str = Query(..., description="Relative artifact path, e.g. screenshots/shot.png"),
    repository: Repository = Depends(get_repository),
    settings: Settings = Depends(get_settings),
) -> dict[str, Any]:
    identifier = _run_id(run_id)
    if not repository.run_exists(identifier):
        raise HTTPException(status_code=404, detail="Publish the run before uploading its media.")

    data = await request.body()
    try:
        record = await run_in_threadpool(
            media.store, repository, settings.media_dir, identifier, path, data
        )
    except media.MediaError as error:
        raise HTTPException(status_code=error.status, detail=str(error)) from error

    return {
        "ok": True,
        "run_id": identifier,
        "path": record["rel_path"],
        "bytes": record["bytes"],
        "animated": bool(record["animated"]),
        "checksum": record["checksum"],
    }


@router.delete("/runs/{run_id}", dependencies=[Depends(require_api_key)])
def delete_run(
    run_id: str,
    repository: Repository = Depends(get_repository),
    settings: Settings = Depends(get_settings),
) -> dict[str, Any]:
    identifier = _run_id(run_id)
    if not repository.run_exists(identifier):
        raise HTTPException(status_code=404, detail=f"Unknown run id: {identifier}")

    repository.delete_run(identifier)
    media.delete_run(settings.media_dir, identifier)

    return {"ok": True, "run_id": identifier, "deleted": True}


# -------------------------------------------------------------------- reads


@router.get("/runs")
def list_runs(
    response: Response,
    status: str = "",
    feature: str = "",
    project: str = "",
    done: str = "",
    q: str = "",
    page: int = 1,
    per_page: int = PER_PAGE_DEFAULT,
    repository: Repository = Depends(get_repository),
) -> dict[str, Any]:
    response.headers["Access-Control-Allow-Origin"] = "*"
    result = repository.query_runs(
        status=status,
        feature=feature,
        project=project,
        done=done,
        search=q,
        page=page,
        per_page=per_page,
    )

    return {
        "runs": [_shape_run(run) for run in result["rows"]],
        "total": result["total"],
        "page": result["page"],
        "pages": result["pages"],
    }


@router.get("/runs/{run_id}/media")
def list_media(
    run_id: str,
    response: Response,
    repository: Repository = Depends(get_repository),
) -> dict[str, Any]:
    response.headers["Access-Control-Allow-Origin"] = "*"
    identifier = _run_id(run_id)

    return {"run_id": identifier, "files": repository.artifact_manifest(identifier)}


@router.get("/runs/{run_id}")
def get_run(
    run_id: str,
    response: Response,
    repository: Repository = Depends(get_repository),
) -> dict[str, Any]:
    response.headers["Access-Control-Allow-Origin"] = "*"
    identifier = _run_id(run_id)

    run = repository.find_run(identifier)
    if run is None:
        raise HTTPException(status_code=404, detail=f"Unknown run id: {identifier}")

    events = repository.events_for(identifier)

    return {
        **_shape_run(run),
        "events": [_shape_event(event) for event in events],
        "stages": workflow.stages(run, events),
        "documents": [
            {"slug": document["slug"], "title": document["title"], "kind": document["kind"]}
            for document in repository.documents_for(identifier)
        ],
        "media": [_shape_artifact(artifact) for artifact in repository.artifacts_for(identifier)],
    }


@router.get("/summary")
def summary(
    response: Response,
    repository: Repository = Depends(get_repository),
) -> dict[str, Any]:
    response.headers["Access-Control-Allow-Origin"] = "*"

    return repository.summary()


@router.get("/healthz")
def healthz(repository: Repository = Depends(get_repository)) -> dict[str, Any]:
    """Liveness probe: proves the process is up and the database is readable."""
    counters = repository.summary()

    return {"ok": True, "runs": counters["total"], "running": counters["running"]}
