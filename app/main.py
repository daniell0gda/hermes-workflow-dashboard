"""Application factory."""

from __future__ import annotations

import logging
from pathlib import Path
from urllib.parse import urlencode

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from . import api, db, formatting, views, workflow
from .config import Settings, load_settings, prepare_data_dir
from .markdown_render import renderer

logger = logging.getLogger("hfcd")

PACKAGE_DIR = Path(__file__).resolve().parent


def build_templates(settings: Settings) -> Jinja2Templates:
    templates = Jinja2Templates(directory=str(PACKAGE_DIR / "templates"))
    templates.env.filters.update(formatting.JINJA_FILTERS)
    templates.env.globals.update(
        {
            "site_name": settings.site_name,
            "url_path": _url_path_builder(settings.root_path),
            "list_url": _list_url_builder(settings.root_path),
            "health_of": formatting.health,
            "display_status": formatting.display_status,
            "run_duration_ms": formatting.run_duration_ms,
            "stage_progress": workflow.progress,
            "render_markdown": renderer.render,
            "dash": formatting.DASH,
        }
    )
    templates.env.trim_blocks = True
    templates.env.lstrip_blocks = True

    return templates


def _url_path_builder(root_path: str):
    """Prefix absolute app paths when served under a reverse-proxy subpath."""

    def url_path(path: str) -> str:
        if not path.startswith("/"):
            return path
        return f"{root_path}{path}"

    return url_path


def _list_url_builder(root_path: str):
    """Build a filtered run-list URL, omitting empty and default parameters."""

    def list_url(status: str = "", q: str = "", project: str = "", done: str = "", pg: int = 1) -> str:
        params = {
            name: value
            for name, value in (("status", status), ("q", q), ("project", project), ("done", done))
            if value
        }
        if pg and int(pg) > 1:
            params["pg"] = str(pg)
        query = urlencode(params)

        return f"{root_path}/" + (f"?{query}" if query else "")

    return list_url


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or load_settings()
    prepare_data_dir(settings)

    # Run migrations once at startup rather than per request.
    startup_connection = db.initialise(settings.database_path)
    startup_connection.close()

    app = FastAPI(
        title="Hermes Feature Check Dashboard",
        version="1.0.0",
        root_path=settings.root_path,
        docs_url="/api/docs",
        openapi_url="/api/openapi.json",
    )
    app.state.settings = settings
    app.state.templates = build_templates(settings)

    app.include_router(api.router)
    app.include_router(views.router)

    app.mount(
        "/static",
        StaticFiles(directory=str(PACKAGE_DIR / "static")),
        name="static",
    )
    app.mount(
        "/media",
        StaticFiles(directory=str(settings.media_dir)),
        name="media",
    )

    if not settings.writes_enabled:
        logger.warning(
            "HFCD_API_KEY is not set: the dashboard is read-only and the worker "
            "cannot publish runs."
        )
    logger.info("data directory: %s", settings.data_dir)

    return app
