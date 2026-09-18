from __future__ import annotations

from datetime import datetime, timezone
from io import BytesIO
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from PIL import Image

from app import db
from app.config import Settings
from app.main import create_app
from app.repository import Repository

API_KEY = "test-api-key"


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings(
        data_dir=tmp_path / "data",
        api_key=API_KEY,
        site_name="Test",
        host="127.0.0.1",
        port=8080,
        root_path="",
    )


@pytest.fixture
def app(settings: Settings):
    return create_app(settings)


@pytest.fixture
def client(app) -> TestClient:
    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture
def auth() -> dict[str, str]:
    return {"X-API-Key": API_KEY}


@pytest.fixture
def repository(settings: Settings) -> Repository:
    settings.media_dir.mkdir(parents=True, exist_ok=True)
    connection = db.initialise(settings.database_path)
    try:
        yield Repository(connection)
    finally:
        connection.close()


def png_bytes(size: tuple[int, int] = (1200, 800), colour: tuple[int, int, int] = (30, 80, 200)) -> bytes:
    buffer = BytesIO()
    Image.new("RGB", size, colour).save(buffer, format="PNG")

    return buffer.getvalue()


def animated_gif_bytes(frames: int = 3) -> bytes:
    """A GIF with genuinely different frames.

    Frames must differ in pixel content, not just in a palette index, or Pillow
    collapses them into a single-frame (non-animated) GIF.
    """
    images = []
    for index in range(frames):
        frame = Image.new("RGB", (40, 40), (0, 0, 0))
        frame.paste((255, 0, 0), (index * 8, 0, index * 8 + 8, 40))
        images.append(frame.convert("P"))

    buffer = BytesIO()
    images[0].save(
        buffer,
        format="GIF",
        save_all=True,
        append_images=images[1:],
        duration=80,
        loop=0,
    )

    return buffer.getvalue()


def still_gif_bytes() -> bytes:
    buffer = BytesIO()
    Image.new("P", (40, 40), 5).save(buffer, format="GIF")

    return buffer.getvalue()


def live_payload(run_id: str = "live-run") -> dict:
    """A running run whose heartbeat is current, so it reads as live not stale."""
    payload = sample_payload(run_id=run_id)
    payload["status"].update(
        {
            "status": "running",
            "ended_at": None,
            "active_node": "code",
            "phase": "code",
            "heartbeat_at": datetime.now(tz=timezone.utc).isoformat(),
        }
    )

    return payload


def abandoned_payload(run_id: str = "stuck-run") -> dict:
    """A running run whose heartbeat expired long ago, so it reads as abandoned."""
    payload = sample_payload(run_id=run_id)
    payload["status"].update(
        {
            "status": "running",
            "ended_at": None,
            "active_node": "code",
            "phase": "code",
            "heartbeat_at": "2020-01-01T00:00:00.000Z",
        }
    )

    return payload


def sample_payload(run_id: str = "demo-run-r1", **overrides) -> dict:
    payload = {
        "run_id": run_id,
        "status": {
            "run_id": run_id,
            "feature": "demo-feature",
            "status": "completed",
            "started_at": "2026-08-25T10:00:00.000Z",
            "ended_at": "2026-08-25T10:20:00.000Z",
            "heartbeat_at": "2026-08-25T10:20:00.000Z",
            "phase": "team-leader",
            "active_node": None,
            "host": "worker-1",
            "error": None,
        },
        "events": [
            {
                "sequence": 1,
                "timestamp": "2026-08-25T10:05:00.000Z",
                "node": "plan",
                "status": "completed",
                "duration_ms": 300000,
                "summary": "Plan written.",
                "error": None,
            },
            {
                "sequence": 2,
                "timestamp": "2026-08-25T10:10:00.000Z",
                "node": "code",
                "status": "completed",
                "duration_ms": 600000,
                "summary": "Implemented.",
                "error": None,
            },
            {
                "sequence": 3,
                "timestamp": "2026-08-25T10:14:00.000Z",
                "node": "check",
                "status": "completed",
                "duration_ms": 120000,
                "summary": "Checked.",
                "error": None,
            },
            {
                "sequence": 4,
                "timestamp": "2026-08-25T10:17:00.000Z",
                "node": "code",
                "status": "completed",
                "duration_ms": 90000,
                "summary": "Revision 1.",
                "error": None,
            },
            {
                "sequence": 5,
                "timestamp": "2026-08-25T10:19:00.000Z",
                "node": "team-leader",
                "status": "completed",
                "duration_ms": 30000,
                "summary": "Gate complete.",
                "error": None,
            },
        ],
        "graph": {
            "nodes": [
                {"id": "starting", "name": "starting"},
                {"id": "planning", "name": "planning"},
                {"id": "implementing", "name": "implementing"},
                {"id": "checking", "name": "checking"},
                {"id": "reporting", "name": "reporting"},
            ],
            "edges": [],
        },
        "metrics": {
            "invocations": [
                {"task_id": "plan", "role": "plan", "model": "m", "cost_usd": 0.0,
                 "cost_status": "unknown", "input_tokens": 1000, "output_tokens": 100},
                {"task_id": "code", "role": "code", "model": "m", "cost_usd": 0.0,
                 "cost_status": "unknown", "input_tokens": 2000, "output_tokens": 250},
            ]
        },
        "documents": [
            {
                "slug": "team-leader.report",
                "kind": "primary",
                "body": "# Team-leader report\n\n- **Result:** completed\n- **Classification:** pass\n",
            },
            {"slug": "plan", "kind": "primary", "body": "# Plan\n\n- do the thing\n"},
            {"slug": "clusters/1-first", "kind": "cluster", "body": "# First cluster\n\ndetail\n"},
        ],
    }
    payload.update(overrides)

    return payload
