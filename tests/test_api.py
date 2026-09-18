from __future__ import annotations

import dataclasses

import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app
from tests.conftest import (
    abandoned_payload,
    animated_gif_bytes,
    live_payload,
    png_bytes,
    sample_payload,
)

RUN_ID = "demo-run-r1"


def publish(client: TestClient, auth: dict, payload: dict | None = None):
    return client.post("/api/runs", json=payload or sample_payload(), headers=auth)


class TestAuthentication:
    def test_publish_without_a_key_is_rejected(self, client: TestClient) -> None:
        assert client.post("/api/runs", json=sample_payload()).status_code == 401

    def test_publish_with_a_wrong_key_is_rejected(self, client: TestClient) -> None:
        response = client.post("/api/runs", json=sample_payload(), headers={"X-API-Key": "nope"})

        assert response.status_code == 401

    @pytest.mark.parametrize(
        ("method", "path"),
        [
            ("post", f"/api/runs/{RUN_ID}/status"),
            ("post", f"/api/runs/{RUN_ID}/heartbeat"),
            ("post", f"/api/runs/{RUN_ID}/media?path=screenshots/a.png"),
            ("delete", f"/api/runs/{RUN_ID}"),
        ],
    )
    def test_every_mutating_route_needs_the_key(self, client: TestClient, method: str, path: str) -> None:
        assert getattr(client, method)(path).status_code == 401

    def test_reads_stay_open(self, client: TestClient) -> None:
        assert client.get("/api/runs").status_code == 200
        assert client.get("/api/summary").status_code == 200

    def test_ingest_is_closed_when_no_key_is_configured(self, settings: Settings) -> None:
        """A server without HFCD_API_KEY must not accept anonymous writes."""
        keyless = create_app(dataclasses.replace(settings, api_key=None))

        with TestClient(keyless) as client:
            response = client.post("/api/runs", json=sample_payload(), headers={"X-API-Key": "x"})

        assert response.status_code == 503


class TestPublish:
    def test_publish_reports_what_it_stored(self, client: TestClient, auth: dict) -> None:
        body = publish(client, auth).json()

        assert body == {"ok": True, "run_id": RUN_ID, "events": 5, "documents": 3}

    def test_published_run_is_readable_with_derived_fields(self, client: TestClient, auth: dict) -> None:
        publish(client, auth)

        run = client.get(f"/api/runs/{RUN_ID}").json()

        assert run["feature"] == "demo-feature"
        assert run["classification"] == "pass"
        assert run["revisions"] == 1
        assert run["duration_ms"] == 1_200_000
        assert run["input_tokens"] == 3000
        assert run["cost_usd"] is None
        assert len(run["events"]) == 5
        assert [stage["key"] for stage in run["stages"]] == ["start", "plan", "code", "check", "report"]
        assert [document["slug"] for document in run["documents"]][0] == "team-leader.report"

    def test_bad_run_id_is_a_client_error(self, client: TestClient, auth: dict) -> None:
        assert client.post("/api/runs", json={"run_id": "//"}, headers=auth).status_code == 400

    def test_unknown_run_is_a_404(self, client: TestClient) -> None:
        assert client.get("/api/runs/never-published").status_code == 404


class TestListing:
    def test_lists_and_paginates(self, client: TestClient, auth: dict) -> None:
        for index in range(3):
            publish(client, auth, sample_payload(run_id=f"run-{index}"))

        body = client.get("/api/runs", params={"per_page": 2}).json()

        assert body["total"] == 3
        assert body["pages"] == 2
        assert len(body["runs"]) == 2

    def test_filters_by_status(self, client: TestClient, auth: dict) -> None:
        publish(client, auth, sample_payload(run_id="done-run"))
        publish(client, auth, live_payload(run_id="live-run"))

        body = client.get("/api/runs", params={"status": "running"}).json()

        assert [run["run_id"] for run in body["runs"]] == ["live-run"]

    def test_filters_by_the_derived_abandoned_status(self, client: TestClient, auth: dict) -> None:
        publish(client, auth, live_payload(run_id="live-run"))
        publish(client, auth, abandoned_payload(run_id="stuck-run"))

        body = client.get("/api/runs", params={"status": "abandoned"}).json()

        assert [run["run_id"] for run in body["runs"]] == ["stuck-run"]
        assert body["runs"][0]["status"] == "running"
        assert body["runs"][0]["display_status"] == "abandoned"

    def test_searches_run_id_and_feature(self, client: TestClient, auth: dict) -> None:
        publish(client, auth, sample_payload(run_id="alpha-run"))
        publish(client, auth, sample_payload(run_id="beta-run"))

        body = client.get("/api/runs", params={"q": "alpha"}).json()

        assert [run["run_id"] for run in body["runs"]] == ["alpha-run"]

    def test_summary_aggregates(self, client: TestClient, auth: dict) -> None:
        publish(client, auth)

        body = client.get("/api/summary").json()

        assert body["total"] == 1
        assert body["passed"] == 1
        assert body["input_tokens"] == 3000


class TestStatusAndHeartbeat:
    def test_patch_updates_and_keeps_derived_values(self, client: TestClient, auth: dict) -> None:
        publish(client, auth)

        response = client.post(f"/api/runs/{RUN_ID}/status", json={"phase": "check"}, headers=auth)

        assert response.json()["phase"] == "check"
        assert client.get(f"/api/runs/{RUN_ID}").json()["revisions"] == 1

    def test_patch_on_unknown_run_is_404(self, client: TestClient, auth: dict) -> None:
        response = client.post("/api/runs/ghost/status", json={"phase": "x"}, headers=auth)

        assert response.status_code == 404

    def test_heartbeat_moves_the_timestamp(self, client: TestClient, auth: dict) -> None:
        publish(client, auth)
        before = client.get(f"/api/runs/{RUN_ID}").json()["heartbeat_at"]

        assert client.post(f"/api/runs/{RUN_ID}/heartbeat", headers=auth).status_code == 200
        assert client.get(f"/api/runs/{RUN_ID}").json()["heartbeat_at"] != before

    def test_heartbeat_on_unknown_run_is_404(self, client: TestClient, auth: dict) -> None:
        assert client.post("/api/runs/ghost/heartbeat", headers=auth).status_code == 404


class TestMedia:
    def upload(self, client: TestClient, auth: dict, path: str, data: bytes):
        return client.post(
            f"/api/runs/{RUN_ID}/media",
            params={"path": path},
            content=data,
            headers={**auth, "Content-Type": "application/octet-stream"},
        )

    def test_upload_then_serve(self, client: TestClient, auth: dict) -> None:
        publish(client, auth)

        response = self.upload(client, auth, "screenshots/a.png", png_bytes())

        assert response.status_code == 200
        assert response.json()["animated"] is False

        served = client.get(f"/media/{RUN_ID}/screenshots/a.png")

        assert served.status_code == 200
        assert served.headers["content-type"] == "image/png"

    def test_manifest_enables_skipping_unchanged_files(self, client: TestClient, auth: dict) -> None:
        publish(client, auth)
        checksum = self.upload(client, auth, "screenshots/a.png", png_bytes()).json()["checksum"]

        body = client.get(f"/api/runs/{RUN_ID}/media").json()

        assert body["files"] == {"screenshots/a.png": checksum}

    def test_animated_gif_is_reported(self, client: TestClient, auth: dict) -> None:
        publish(client, auth)

        body = self.upload(client, auth, "screenshots/clip.gif", animated_gif_bytes()).json()

        assert body["animated"] is True

    def test_upload_before_publish_is_404(self, client: TestClient, auth: dict) -> None:
        assert self.upload(client, auth, "screenshots/a.png", png_bytes()).status_code == 404

    def test_mismatched_content_is_415(self, client: TestClient, auth: dict) -> None:
        publish(client, auth)

        assert self.upload(client, auth, "screenshots/a.png", b"nonsense").status_code == 415

    def test_media_appears_on_the_run_payload(self, client: TestClient, auth: dict) -> None:
        publish(client, auth)
        self.upload(client, auth, "screenshots/a.png", png_bytes())

        media = client.get(f"/api/runs/{RUN_ID}").json()["media"]

        assert media[0]["path"] == "screenshots/a.png"
        assert media[0]["url"] == f"/media/{RUN_ID}/screenshots/a.png"


class TestDelete:
    def test_delete_removes_run_and_files(self, client: TestClient, auth: dict, settings: Settings) -> None:
        publish(client, auth)
        client.post(
            f"/api/runs/{RUN_ID}/media",
            params={"path": "screenshots/a.png"},
            content=png_bytes(),
            headers={**auth, "Content-Type": "application/octet-stream"},
        )

        assert client.delete(f"/api/runs/{RUN_ID}", headers=auth).status_code == 200
        assert client.get(f"/api/runs/{RUN_ID}").status_code == 404
        assert not (settings.media_dir / RUN_ID).exists()

    def test_delete_unknown_run_is_404(self, client: TestClient, auth: dict) -> None:
        assert client.delete("/api/runs/ghost", headers=auth).status_code == 404


class TestHealth:
    def test_healthz_reports_counts(self, client: TestClient, auth: dict) -> None:
        publish(client, auth)

        assert client.get("/api/healthz").json() == {"ok": True, "runs": 1, "running": 0}
