"""Shared request dependencies: settings, database session, write auth."""

from __future__ import annotations

import secrets
from typing import Iterator

from fastapi import Depends, Header, HTTPException, Request

from . import db
from .config import Settings
from .repository import Repository

API_KEY_HEADER = "X-API-Key"


def get_settings(request: Request) -> Settings:
    return request.app.state.settings


def get_repository(settings: Settings = Depends(get_settings)) -> Iterator[Repository]:
    """One SQLite connection per request.

    Cheap for SQLite, and it keeps each worker thread on its own connection
    instead of sharing one across the thread pool.
    """
    connection = db.connect(settings.database_path)
    try:
        yield Repository(connection)
    finally:
        connection.close()


def require_api_key(
    settings: Settings = Depends(get_settings),
    x_api_key: str | None = Header(default=None, alias=API_KEY_HEADER),
) -> None:
    """Guards every mutating route. Reads are deliberately open."""
    if not settings.writes_enabled:
        raise HTTPException(
            status_code=503,
            detail="Ingest is disabled: HFCD_API_KEY is not set on the server.",
        )

    if not x_api_key or not secrets.compare_digest(x_api_key, settings.api_key or ""):
        raise HTTPException(
            status_code=401,
            detail=f"Missing or invalid {API_KEY_HEADER} header.",
            headers={"WWW-Authenticate": API_KEY_HEADER},
        )
