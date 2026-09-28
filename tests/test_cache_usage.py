"""Prompt-cache usage: tokens read from and written to the cache, per call and per run.

The worker's cost already prices cache reads and writes, and its input_tokens
leave cached tokens out, so the cache figures stand beside the cost rather than
adding to it.
"""

from __future__ import annotations

from fastapi.testclient import TestClient

from app import db, ingest
from app.repository import Repository
from tests.conftest import sample_payload

RUN_ID = "demo-run-r1"


def cached_payload(run_id: str = RUN_ID) -> dict:
    """The sample run with both calls reporting cache usage and a resolved price."""
    payload = sample_payload(run_id=run_id)
    payload["metrics"]["invocations"][0].update(
        {"cache_read_tokens": 40_000, "cache_write_tokens": 5_000,
         "cost_usd": 0.25, "cost_status": "estimated"}
    )
    payload["metrics"]["invocations"][1].update(
        {"cache_read_tokens": 60_000, "cache_write_tokens": None,
         "cost_usd": 0.75, "cost_status": "estimated"}
    )

    return payload


def publish_to(repository: Repository, payload: dict) -> dict:
    with repository.transaction():
        ingest.publish(repository, payload["run_id"], payload)

    return repository.find_run(payload["run_id"])


def publish(client: TestClient, auth: dict, payload: dict) -> None:
    assert client.post("/api/runs", json=payload, headers=auth).status_code == 200


class TestIngest:
    def test_cache_tokens_are_summed_from_the_calls(self, repository: Repository) -> None:
        run = publish_to(repository, cached_payload())

        assert run["cache_read_tokens"] == 100_000

    def test_a_call_without_a_figure_is_left_out_of_the_sum(self, repository: Repository) -> None:
        run = publish_to(repository, cached_payload())

        assert run["cache_write_tokens"] == 5_000

    def test_no_call_reporting_the_cache_leaves_it_unknown(self, repository: Repository) -> None:
        run = publish_to(repository, sample_payload())

        assert (run["cache_read_tokens"], run["cache_write_tokens"]) == (None, None)

    def test_a_stated_run_total_wins_over_the_calls(self, repository: Repository) -> None:
        payload = cached_payload()
        payload["metrics"]["cache_read_tokens"] = 7

        assert publish_to(repository, payload)["cache_read_tokens"] == 7

    def test_the_cost_is_not_topped_up_with_the_cache(self, repository: Repository) -> None:
        run = publish_to(repository, cached_payload())

        assert run["cost_usd"] == 1.0


class TestMigration:
    def test_an_existing_v4_database_gains_the_columns(self, tmp_path) -> None:
        path = tmp_path / "old.sqlite3"
        connection = db.connect(path)
        for script in db._MIGRATIONS[:4]:
            connection.executescript(script)
        connection.execute("INSERT INTO runs (run_id, created_at, updated_at) VALUES ('a', '', '')")
        connection.execute("PRAGMA user_version = 4")
        connection.close()

        upgraded = db.initialise(path)
        existing = upgraded.execute("SELECT * FROM runs WHERE run_id = 'a'").fetchone()
        version = upgraded.execute("PRAGMA user_version").fetchone()[0]
        upgraded.close()

        assert (existing["cache_read_tokens"], existing["cache_write_tokens"]) == (None, None)
        assert version == db.SCHEMA_VERSION


class TestApi:
    def test_the_run_reports_its_cache_tokens(self, client: TestClient, auth: dict) -> None:
        publish(client, auth, cached_payload())

        run = client.get(f"/api/runs/{RUN_ID}").json()

        assert (run["cache_read_tokens"], run["cache_write_tokens"]) == (100_000, 5_000)

    def test_the_summary_totals_the_cache_tokens(self, client: TestClient, auth: dict) -> None:
        publish(client, auth, cached_payload())
        publish(client, auth, cached_payload(run_id="second-run"))

        summary = client.get("/api/summary").json()

        assert (summary["cache_read_tokens"], summary["cache_write_tokens"]) == (200_000, 10_000)


class TestRunView:
    def test_the_overview_shows_the_cache_tokens(self, client: TestClient, auth: dict) -> None:
        publish(client, auth, cached_payload())

        body = client.get(f"/run/{RUN_ID}").text

        assert "Cache read" in body
        assert "100.0k" in body
        assert "already includes cache pricing" in body

    def test_the_usage_table_prices_and_caches_each_call(
        self, client: TestClient, auth: dict
    ) -> None:
        publish(client, auth, cached_payload())

        body = client.get(f"/run/{RUN_ID}").text

        assert "40,000" in body
        assert "$0.25" in body

    def test_an_unpriced_call_shows_no_cost(self, client: TestClient, auth: dict) -> None:
        # The sample calls carry cost_status: unknown with a placeholder 0.0.
        publish(client, auth, sample_payload())

        assert "$0.00" not in client.get(f"/run/{RUN_ID}").text

    def test_no_cache_tiles_when_the_worker_reports_none(
        self, client: TestClient, auth: dict
    ) -> None:
        publish(client, auth, sample_payload())

        assert "Cache read" not in client.get(f"/run/{RUN_ID}").text


class TestListView:
    def test_the_totals_show_the_cache_tokens(self, client: TestClient, auth: dict) -> None:
        publish(client, auth, cached_payload())

        body = client.get("/").text

        assert "Cache read" in body
        assert "Cache write" in body

    def test_no_cache_tiles_without_cache_data(self, client: TestClient, auth: dict) -> None:
        publish(client, auth, sample_payload())

        assert "Cache read" not in client.get("/").text
