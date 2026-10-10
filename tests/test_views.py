from __future__ import annotations

import hashlib
import re

import pytest
from fastapi.testclient import TestClient

from tests.conftest import (
    abandoned_payload,
    animated_gif_bytes,
    live_payload,
    png_bytes,
    sample_payload,
)

RUN_ID = "demo-run-r1"


def publish(client: TestClient, auth: dict, payload: dict | None = None) -> None:
    response = client.post("/api/runs", json=payload or sample_payload(), headers=auth)
    assert response.status_code == 200


def upload(client: TestClient, auth: dict, path: str, data: bytes) -> None:
    response = client.post(
        f"/api/runs/{RUN_ID}/media",
        params={"path": path},
        content=data,
        headers={**auth, "Content-Type": "application/octet-stream"},
    )
    assert response.status_code == 200


def reports_key(body: str) -> str:
    match = re.search(r'data-live="reports" data-live-key="([^"]*)"', body)
    assert match, "the reports region carries no stage key"

    return match.group(1)


class TestRunsList:
    def test_renders_with_no_runs(self, client: TestClient) -> None:
        response = client.get("/")

        assert response.status_code == 200
        assert "Run dashboard" in response.text
        assert "No runs match this view" in response.text

    def test_shows_a_published_run(self, client: TestClient, auth: dict) -> None:
        publish(client, auth)

        body = client.get("/").text

        assert "demo-feature" in body
        assert RUN_ID in body
        assert "completed" in body
        assert "pass" in body
        assert "1 rev" in body

    def test_running_run_gets_a_live_card(self, client: TestClient, auth: dict) -> None:
        publish(client, auth, live_payload())

        body = client.get("/").text

        assert "Running now" in body
        assert 'class="pulse"' in body
        assert "Implement" in body

    def test_abandoned_run_is_not_running_now(self, client: TestClient, auth: dict) -> None:
        publish(client, auth, abandoned_payload())

        body = client.get("/").text

        # No live-run panel and no pulsing badge: it is listed as abandoned.
        assert 'data-live="active"' not in body
        assert 'class="pulse"' not in body
        assert "abandoned" in body

    def test_abandoned_run_is_left_out_of_the_running_count(
        self, client: TestClient, auth: dict
    ) -> None:
        publish(client, auth, abandoned_payload())
        publish(client, auth, live_payload())

        assert "is-stuck" in client.get("/").text

        summary = client.get("/api/summary").json()

        assert summary["running"] == 1
        assert summary["abandoned"] == 1

    def test_abandoned_filter_selects_only_stuck_runs(self, client: TestClient, auth: dict) -> None:
        publish(client, auth, abandoned_payload())
        publish(client, auth, sample_payload(run_id="done-run"))

        body = client.get("/", params={"status": "abandoned"}).text

        assert "stuck-run" in body
        assert "done-run" not in body

    def test_running_filter_excludes_stuck_runs(self, client: TestClient, auth: dict) -> None:
        publish(client, auth, abandoned_payload())
        publish(client, auth, live_payload())

        body = client.get("/", params={"status": "running"}).text

        assert "live-run" in body
        assert "stuck-run" not in body

    def test_status_filter_narrows_the_table(self, client: TestClient, auth: dict) -> None:
        publish(client, auth)

        body = client.get("/", params={"status": "failed"}).text

        assert "No runs match this view" in body

    def test_search_filter_applies(self, client: TestClient, auth: dict) -> None:
        publish(client, auth)

        assert "demo-feature" in client.get("/", params={"q": "demo"}).text
        assert "No runs match" in client.get("/", params={"q": "zzzz"}).text

    def test_is_not_indexable(self, client: TestClient) -> None:
        assert "noindex" in client.get("/").headers["x-robots-tag"]


class TestRunDetail:
    def test_renders_every_section(self, client: TestClient, auth: dict) -> None:
        publish(client, auth)

        body = client.get(f"/run/{RUN_ID}").text

        for anchor in ('id="overview"', 'id="workflow"', 'id="timeline"', 'id="usage"', 'id="reports"'):
            assert anchor in body

    def test_shows_stage_labels_and_states(self, client: TestClient, auth: dict) -> None:
        publish(client, auth)

        body = client.get(f"/run/{RUN_ID}").text

        assert "Implement" in body
        assert "is-done" in body

    def test_shows_one_bar_per_pass_of_a_repeated_stage(self, client: TestClient, auth: dict) -> None:
        publish(client, auth)

        body = client.get(f"/run/{RUN_ID}").text

        # The demo run codes twice, so the Implement stage carries two bars.
        assert body.count('class="stage-pass is-done"') == 2

    def test_renders_report_markdown(self, client: TestClient, auth: dict) -> None:
        publish(client, auth)

        body = client.get(f"/run/{RUN_ID}").text

        assert "Team-leader report" in body
        assert "<strong>Classification:</strong>" in body

    def test_shows_the_usage_table(self, client: TestClient, auth: dict) -> None:
        publish(client, auth)

        body = client.get(f"/run/{RUN_ID}").text

        assert "1,000" in body
        assert "3,000" in body
        assert "cost_status: unknown" in body

    def test_gallery_lists_screenshots_and_flags_gifs(self, client: TestClient, auth: dict) -> None:
        publish(client, auth)
        upload(client, auth, "screenshots/shot.png", png_bytes())
        upload(client, auth, "screenshots/clip.gif", animated_gif_bytes())

        body = client.get(f"/run/{RUN_ID}").text

        assert 'id="screenshots"' in body
        assert "shot.png" in body
        assert "shot-flag" in body

    def test_no_gallery_section_without_media(self, client: TestClient, auth: dict) -> None:
        publish(client, auth)

        assert 'id="screenshots"' not in client.get(f"/run/{RUN_ID}").text

    def test_report_image_resolves_to_the_uploaded_file(self, client: TestClient, auth: dict) -> None:
        payload = sample_payload()
        payload["documents"] = [
            {
                "slug": "manual-report",
                "kind": "primary",
                "body": "# Manual\n\n![wide](screenshots/shot.png)\n",
            }
        ]
        publish(client, auth, payload)
        upload(client, auth, "screenshots/shot.png", png_bytes())

        body = client.get(f"/run/{RUN_ID}").text

        assert f'src="/media/{RUN_ID}/screenshots/shot.png"' in body

    def test_report_image_without_an_upload_degrades(self, client: TestClient, auth: dict) -> None:
        payload = sample_payload()
        payload["documents"] = [
            {"slug": "manual-report", "kind": "primary", "body": "![wide](screenshots/absent.png)\n"}
        ]
        publish(client, auth, payload)

        body = client.get(f"/run/{RUN_ID}").text

        assert "hfcd-missing-media" in body
        assert "screenshots/absent.png" not in body

    def test_stale_running_run_is_flagged_abandoned(self, client: TestClient, auth: dict) -> None:
        publish(client, auth, abandoned_payload(run_id="stuck-run"))

        body = client.get("/run/stuck-run").text

        assert "abandoned" in body.lower()
        assert "no heartbeat for" in body

    def test_live_run_is_not_flagged(self, client: TestClient, auth: dict) -> None:
        publish(client, auth, live_payload(run_id="fresh-run"))

        assert "abandoned" not in client.get("/run/fresh-run").text.lower()

    def test_active_stage_carries_a_clock_from_the_last_call(
        self, client: TestClient, auth: dict
    ) -> None:
        publish(client, auth, live_payload(run_id="fresh-run"))

        body = client.get("/run/fresh-run").text

        # The last event landed at 10:19, so that is when the active pass began.
        assert 'class="stage-now" data-elapsed datetime="2026-08-25T10:19:00+00:00"' in body

    def test_finished_run_has_no_clock(self, client: TestClient, auth: dict) -> None:
        publish(client, auth)

        assert "data-elapsed" not in client.get(f"/run/{RUN_ID}").text

    def test_reports_region_is_ready_before_the_first_report(
        self, client: TestClient, auth: dict
    ) -> None:
        payload = live_payload(run_id="fresh-run")
        payload["documents"] = []
        publish(client, auth, payload)

        body = client.get("/run/fresh-run").text

        assert 'data-live="reports"' in body
        assert 'id="reports"' not in body

    def test_reports_region_is_rekeyed_when_the_stage_moves(
        self, client: TestClient, auth: dict
    ) -> None:
        payload = live_payload(run_id="fresh-run")
        publish(client, auth, payload)
        coding = reports_key(client.get("/run/fresh-run").text)

        payload["status"].update({"active_node": "check", "phase": "check"})
        publish(client, auth, payload)

        assert reports_key(client.get("/run/fresh-run").text) != coding

    def test_every_report_opens_full_page_under_its_title(
        self, client: TestClient, auth: dict
    ) -> None:
        publish(client, auth)

        body = client.get(f"/run/{RUN_ID}").text

        # Two primary tabs and one work cluster.
        for title in ("Team-leader report", "Plan", "First"):
            assert f'data-reader="{title}"' in body

    def test_a_stage_summary_opens_full_page_under_its_stage(
        self, client: TestClient, auth: dict
    ) -> None:
        publish(client, auth)

        # Named as the timeline row names it.
        assert 'data-reader="Code · call #2"' in client.get(f"/run/{RUN_ID}").text

    def test_a_stage_summary_opens_full_page_on_the_report_of_that_call(
        self, client: TestClient, auth: dict
    ) -> None:
        payload = sample_payload()
        payload["documents"].append(
            {"slug": "coder-reports/1-first", "kind": "coder", "body": "# Coder report\n"}
        )
        publish(client, auth, payload)

        body = client.get(f"/run/{RUN_ID}").text

        assert 'data-reader="Code · call #4" data-reader-reports="coder-reports/1-first"' in body
        assert 'data-report="coder-reports/1-first"' in body
        assert 'data-reader="Code · call #2" aria-label' in body

    def test_everything_rendered_as_markdown_can_open_full_page(
        self, client: TestClient, auth: dict
    ) -> None:
        publish(client, auth)

        body = client.get(f"/run/{RUN_ID}").text

        assert body.count("data-reader=") == body.count('<div class="markdown">')

    def test_a_call_without_a_summary_offers_nothing_to_open(
        self, client: TestClient, auth: dict
    ) -> None:
        payload = sample_payload()
        payload["events"][0]["summary"] = ""
        publish(client, auth, payload)

        assert "call #1" not in client.get(f"/run/{RUN_ID}").text

    def test_unknown_run_renders_a_404_page(self, client: TestClient) -> None:
        response = client.get("/run/never-published")

        assert response.status_code == 404
        assert "Run not found" in response.text

    @pytest.mark.parametrize(
        "encoded",
        ["%2e%2e", "%2E%2E%2F%2E%2E%2Fetc%2Fpasswd", "....etcpasswd", "%00"],
    )
    def test_traversal_style_run_ids_are_not_served(self, client: TestClient, encoded: str) -> None:
        response = client.get(f"/run/{encoded}")

        assert response.status_code in (400, 404)
        assert "passwd" not in response.text or "Run not found" in response.text


class TestAssets:
    def test_stylesheet_and_script_are_served(self, client: TestClient) -> None:
        assert client.get("/static/dashboard.css").status_code == 200
        assert client.get("/static/dashboard.js").status_code == 200

    @pytest.mark.parametrize("name", ["dashboard.css", "dashboard.js"])
    def test_pages_link_assets_by_a_hash_of_their_content(self, client: TestClient, name: str) -> None:
        content = client.get(f"/static/{name}").content
        version = hashlib.sha256(content).hexdigest()[:12]

        assert f'/static/{name}?v={version}"' in client.get("/").text
