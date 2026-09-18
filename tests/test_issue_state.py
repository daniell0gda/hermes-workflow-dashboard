"""The forge's state for the issue a run came from.

``done`` is never sent by the caller: it is derived from ``gh_status``, so the
flag and the state it summarises can never disagree.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app import db, ingest
from app.repository import Repository
from tests.conftest import abandoned_payload, sample_payload

RUN_ID = "demo-run-r1"
ISSUE_PATH = f"/api/runs/{RUN_ID}/issue"


def publish(client: TestClient, auth: dict, payload: dict | None = None) -> None:
    response = client.post("/api/runs", json=payload or sample_payload(), headers=auth)
    assert response.status_code == 200


def set_state(client: TestClient, auth: dict, gh_status: str, run_id: str = RUN_ID):
    return client.post(f"/api/runs/{run_id}/issue", json={"gh_status": gh_status}, headers=auth)


def seeded(client: TestClient, auth: dict) -> None:
    """Two runs whose issues are closed, one still open, one never reported."""
    for run_id, gh_status in (("closed-a", "closed"), ("closed-b", "merged"), ("open-a", "open")):
        publish(client, auth, sample_payload(run_id=run_id))
        assert set_state(client, auth, gh_status, run_id=run_id).status_code == 200

    publish(client, auth, sample_payload(run_id="silent-a"))


def publish_run(repository: Repository, payload: dict | None = None) -> None:
    with repository.transaction():
        ingest.publish(repository, RUN_ID, payload or sample_payload())


def store(repository: Repository, gh_status: str, **extra) -> dict:
    if not repository.run_exists(RUN_ID):
        publish_run(repository)

    with repository.transaction():
        return ingest.set_issue_state(repository, RUN_ID, {"gh_status": gh_status, **extra})


class TestDerivation:
    @pytest.mark.parametrize("gh_status", ["closed", "merged", "Closed", "MERGED"])
    def test_a_closed_issue_is_done(self, repository: Repository, gh_status: str) -> None:
        run = store(repository, gh_status)

        assert (run["gh_status"], run["done"]) == (gh_status, 1)

    @pytest.mark.parametrize("gh_status", ["open", "reopened", "triage"])
    def test_anything_else_is_not_done(self, repository: Repository, gh_status: str) -> None:
        assert store(repository, gh_status)["done"] == 0

    def test_reopening_clears_done_again(self, repository: Repository) -> None:
        store(repository, "closed")

        assert store(repository, "reopened")["done"] == 0

    def test_a_given_done_flag_is_ignored(self, repository: Repository) -> None:
        """Only gh_status decides; a caller cannot mark an open issue done."""
        run = store(repository, "open", done=True)

        assert run["done"] == 0

    def test_gh_status_is_required(self, repository: Repository) -> None:
        publish_run(repository)

        with pytest.raises(ingest.IngestError, match="gh_status"):
            with repository.transaction():
                ingest.set_issue_state(repository, RUN_ID, {})

    def test_an_unknown_run_is_refused(self, repository: Repository) -> None:
        with pytest.raises(LookupError):
            with repository.transaction():
                ingest.set_issue_state(repository, "never-published", {"gh_status": "closed"})

    def test_republishing_the_run_keeps_the_issue_state(self, repository: Repository) -> None:
        """A publish writes only the columns it derives, so this survives."""
        store(repository, "closed")

        with repository.transaction():
            ingest.publish(repository, RUN_ID, sample_payload())

        run = repository.find_run(RUN_ID)

        assert (run["gh_status"], run["done"]) == ("closed", 1)


class TestEndpoint:
    def test_without_a_key_it_is_rejected(self, client: TestClient) -> None:
        assert client.post(ISSUE_PATH, json={"gh_status": "closed"}).status_code == 401

    def test_it_reports_what_it_stored(self, client: TestClient, auth: dict) -> None:
        publish(client, auth)

        body = set_state(client, auth, "closed").json()

        assert body == {"ok": True, "run_id": RUN_ID, "gh_status": "closed", "done": True}

    def test_an_unknown_run_is_a_404(self, client: TestClient, auth: dict) -> None:
        assert set_state(client, auth, "closed", run_id="nope").status_code == 404

    def test_a_missing_gh_status_is_a_400(self, client: TestClient, auth: dict) -> None:
        publish(client, auth)

        assert client.post(ISSUE_PATH, json={}, headers=auth).status_code == 400

    def test_it_does_not_revive_an_abandoned_run(self, client: TestClient, auth: dict) -> None:
        """Polling the forge says nothing about whether the worker is alive."""
        publish(client, auth, abandoned_payload(run_id=RUN_ID))

        set_state(client, auth, "closed")

        assert client.get(f"/api/runs/{RUN_ID}").json()["display_status"] == "abandoned"

    def test_the_api_exposes_the_state(self, client: TestClient, auth: dict) -> None:
        publish(client, auth)
        set_state(client, auth, "closed")

        run = client.get(f"/api/runs/{RUN_ID}").json()

        assert (run["gh_status"], run["done"]) == ("closed", True)

    def test_an_unreported_issue_reads_as_not_done(self, client: TestClient, auth: dict) -> None:
        publish(client, auth)

        run = client.get(f"/api/runs/{RUN_ID}").json()

        assert (run["gh_status"], run["done"]) == (None, False)


class TestFiltering:
    def test_done_runs_only(self, client: TestClient, auth: dict) -> None:
        seeded(client, auth)

        body = client.get("/api/runs", params={"done": "yes"}).json()

        assert sorted(run["run_id"] for run in body["runs"]) == ["closed-a", "closed-b"]

    def test_not_done_covers_open_and_unreported(self, client: TestClient, auth: dict) -> None:
        seeded(client, auth)

        body = client.get("/api/runs", params={"done": "no"}).json()

        assert sorted(run["run_id"] for run in body["runs"]) == ["open-a", "silent-a"]

    def test_an_unrecognised_value_filters_nothing(self, client: TestClient, auth: dict) -> None:
        seeded(client, auth)

        assert client.get("/api/runs", params={"done": "maybe"}).json()["total"] == 4

    def test_the_filter_combines_with_status(self, client: TestClient, auth: dict) -> None:
        seeded(client, auth)

        body = client.get("/api/runs", params={"done": "yes", "status": "failed"}).json()

        assert body["total"] == 0

    def test_the_summary_counts_closed_issues(self, client: TestClient, auth: dict) -> None:
        seeded(client, auth)

        summary = client.get("/api/summary").json()

        assert (summary["done"], summary["issue_reported"]) == (2, 3)


class TestListView:
    def test_the_column_and_chips_appear_once_a_state_is_reported(
        self, client: TestClient, auth: dict
    ) -> None:
        seeded(client, auth)

        page = client.get("/").text

        assert '<th scope="col">Issue</th>' in page
        assert "chips-done" in page
        assert "Issues closed" in page

    def test_nothing_is_added_while_no_state_is_reported(self, client: TestClient, auth: dict) -> None:
        publish(client, auth)

        page = client.get("/").text

        assert '<th scope="col">Issue</th>' not in page
        assert "chips-done" not in page
        assert "Issues closed" not in page

    def test_filtering_the_list_by_done(self, client: TestClient, auth: dict) -> None:
        seeded(client, auth)

        page = client.get("/", params={"done": "yes"}).text

        assert "closed-a" in page
        assert "open-a" not in page

    def test_the_done_chip_keeps_the_other_filters(self, client: TestClient, auth: dict) -> None:
        seeded(client, auth)

        page = client.get("/", params={"status": "completed"}).text

        assert "status=completed&amp;done=yes" in page


class TestRunView:
    def test_the_run_page_tags_a_closed_issue(self, client: TestClient, auth: dict) -> None:
        publish(client, auth)
        set_state(client, auth, "closed")

        assert "Issue closed" in client.get(f"/run/{RUN_ID}").text

    def test_the_run_page_tags_an_open_issue(self, client: TestClient, auth: dict) -> None:
        publish(client, auth)
        set_state(client, auth, "open")

        assert "Issue open" in client.get(f"/run/{RUN_ID}").text

    def test_no_tag_without_a_reported_state(self, client: TestClient, auth: dict) -> None:
        publish(client, auth)

        assert "issue-state" not in client.get(f"/run/{RUN_ID}").text


class TestMigration:
    def test_an_existing_v3_database_gains_the_columns(self, tmp_path) -> None:
        path = tmp_path / "old.sqlite3"
        connection = db.connect(path)
        connection.executescript(db._SCHEMA_V1)
        connection.executescript(db._SCHEMA_V2)
        connection.executescript(db._SCHEMA_V3)
        connection.execute("INSERT INTO runs (run_id, created_at, updated_at) VALUES ('a', '', '')")
        connection.execute("PRAGMA user_version = 3")
        connection.close()

        upgraded = db.initialise(path)
        columns = {row["name"] for row in upgraded.execute("PRAGMA table_info(runs)").fetchall()}
        existing = upgraded.execute("SELECT * FROM runs WHERE run_id = 'a'").fetchone()
        version = upgraded.execute("PRAGMA user_version").fetchone()[0]
        upgraded.close()

        assert {"gh_status", "done"} <= columns
        assert (existing["gh_status"], existing["done"]) == (None, 0)
        assert version == db.SCHEMA_VERSION
