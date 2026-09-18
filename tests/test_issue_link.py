from __future__ import annotations

import dataclasses

import pytest
from fastapi.testclient import TestClient

from app import db, ingest
from app.config import ConfigError, IssueLinking, Settings, load_settings
from app.main import create_app
from app.repository import Repository
from tests.conftest import sample_payload

RUN_ID = "demo-run-r1"
TEMPLATE = "https://github.com/daniell0gda/poke-defense-godot/issues/{number}"
OWNER_TEMPLATE = "https://github.com/daniell0gda/{project}/issues/{number}"
ISSUE_URL = "https://github.com/daniell0gda/poke-defense-godot/issues/110"
OTHER_ISSUE_URL = "https://github.com/daniell0gda/piwotworki/issues/7"


def publish(
    repository: Repository,
    payload: dict,
    linking: IssueLinking | None = None,
) -> dict:
    run_id = ingest.sanitize_run_id(payload["run_id"])
    with repository.transaction():
        ingest.publish(repository, run_id, payload, linking)

    return repository.find_run(run_id)


def with_documents(*documents: dict) -> dict:
    payload = sample_payload()
    payload["documents"] = list(documents)

    return payload


class TestTemplateConfiguration:
    def test_absent_by_default(self) -> None:
        linking = load_settings({}).issue_linking

        assert linking.default_template is None
        assert linking.per_project == {}
        assert linking.configured is False

    def test_valid_template_is_kept(self) -> None:
        assert load_settings({"HFCD_ISSUE_URL_TEMPLATE": TEMPLATE}).issue_linking.default_template == TEMPLATE

    def test_template_without_placeholder_is_refused(self) -> None:
        """Otherwise every run would silently link to the same wrong issue."""
        with pytest.raises(ConfigError, match=r"\{number\}"):
            load_settings({"HFCD_ISSUE_URL_TEMPLATE": "https://github.com/o/r/issues/1"})

    def test_non_http_template_is_refused(self) -> None:
        with pytest.raises(ConfigError, match="http"):
            load_settings({"HFCD_ISSUE_URL_TEMPLATE": "javascript:alert({number})"})

    def test_per_project_map_is_parsed(self) -> None:
        raw = (
            '{"poke-defense-godot": "https://github.com/daniell0gda/poke-defense-godot/issues/{number}",'
            ' "piwotworki": "https://gitea.lan/dan/piwotworki/issues/{number}"}'
        )
        linking = load_settings({"HFCD_ISSUE_URL_TEMPLATES": raw}).issue_linking

        assert set(linking.per_project) == {"poke-defense-godot", "piwotworki"}
        assert linking.template_for("piwotworki").startswith("https://gitea.lan/")

    def test_per_project_entry_is_validated(self) -> None:
        with pytest.raises(ConfigError, match="piwotworki"):
            load_settings({"HFCD_ISSUE_URL_TEMPLATES": '{"piwotworki": "https://x/issues/1"}'})

    def test_malformed_json_is_refused(self) -> None:
        with pytest.raises(ConfigError, match="JSON object"):
            load_settings({"HFCD_ISSUE_URL_TEMPLATES": "not json"})

    def test_json_array_is_refused(self) -> None:
        with pytest.raises(ConfigError, match="JSON object"):
            load_settings({"HFCD_ISSUE_URL_TEMPLATES": "[1, 2]"})

    def test_per_project_wins_over_the_default(self) -> None:
        linking = IssueLinking(default_template=TEMPLATE, per_project={"piwotworki": OTHER_ISSUE_URL})

        assert linking.template_for("piwotworki") == OTHER_ISSUE_URL
        assert linking.template_for("something-else") == TEMPLATE
        assert linking.template_for(None) == TEMPLATE


class TestResolutionOrder:
    def test_explicit_payload_url_wins(self, repository: Repository) -> None:
        payload = with_documents(
            {"slug": "request", "kind": "primary", "body": f"see {ISSUE_URL}\n"}
        )
        payload["issue_url"] = "https://example.com/tracker/issues/999"

        run = publish(repository, payload)

        assert run["issue_url"] == "https://example.com/tracker/issues/999"
        assert run["issue_number"] == 999

    def test_url_in_request_document(self, repository: Repository) -> None:
        run = publish(
            repository,
            with_documents({"slug": "request", "kind": "primary", "body": f"- Issue: {ISSUE_URL}\n"}),
        )

        assert run["issue_url"] == ISSUE_URL
        assert run["issue_number"] == 110

    def test_request_document_is_preferred_over_others(self, repository: Repository) -> None:
        run = publish(
            repository,
            with_documents(
                {"slug": "check", "kind": "primary", "body": "https://github.com/o/r/issues/777\n"},
                {"slug": "request", "kind": "primary", "body": f"{ISSUE_URL}\n"},
            ),
        )

        assert run["issue_number"] == 110

    def test_url_in_another_document_is_a_fallback(self, repository: Repository) -> None:
        run = publish(
            repository,
            with_documents({"slug": "report", "kind": "primary", "body": "fixes https://github.com/o/r/issues/42\n"}),
        )

        assert run["issue_number"] == 42

    def test_heading_number_with_template(self, repository: Repository) -> None:
        run = publish(
            repository,
            with_documents({"slug": "request", "kind": "primary", "body": "# Request: #116 game-ready (r6)\n"}),
            IssueLinking(default_template=TEMPLATE),
        )

        assert run["issue_number"] == 116
        assert run["issue_url"] == "https://github.com/daniell0gda/poke-defense-godot/issues/116"

    def test_heading_number_without_template_stays_unlinked(self, repository: Repository) -> None:
        """The number is still useful; a guessed URL would not be."""
        run = publish(
            repository,
            with_documents({"slug": "request", "kind": "primary", "body": "# Request: issue #110 — grass\n"}),
        )

        assert run["issue_number"] == 110
        assert run["issue_url"] is None

    def test_nothing_found_leaves_both_empty(self, repository: Repository) -> None:
        run = publish(
            repository,
            with_documents({"slug": "request", "kind": "primary", "body": "# Request: grass tweak\n"}),
            IssueLinking(default_template=TEMPLATE),
        )

        assert (run["issue_number"], run["issue_url"]) == (None, None)

    def test_run_id_number_is_never_used(self, repository: Repository) -> None:
        """Run ids embed timestamps and revision counters, so guessing is unsafe."""
        payload = with_documents({"slug": "request", "kind": "primary", "body": "no issue here\n"})
        payload["run_id"] = "heart-hud-beat-139-1787422828"

        run = publish(repository, payload, IssueLinking(default_template=TEMPLATE))

        assert run["issue_number"] is None


class TestUrlShapes:
    @pytest.mark.parametrize(
        ("body", "expected"),
        [
            ("https://github.com/o/r/issues/7", 7),
            ("[#7](https://github.com/o/r/issues/7)", 7),
            ("see <https://gitea.example.com/o/r/issues/7>", 7),
            ("https://gitlab.com/o/r/-/issues/7", 7),
            ("http://localhost:3000/o/r/issues/7 done", 7),
            ("(https://github.com/o/r/issues/7)", 7),
        ],
    )
    def test_recognised_forms(self, repository: Repository, body: str, expected: int) -> None:
        run = publish(repository, with_documents({"slug": "request", "kind": "primary", "body": body}))

        assert run["issue_number"] == expected

    def test_unsafe_scheme_in_payload_is_dropped(self, repository: Repository) -> None:
        payload = with_documents({"slug": "request", "kind": "primary", "body": "nothing\n"})
        payload["issue_url"] = "javascript:alert(1)"

        assert publish(repository, payload)["issue_url"] is None

    def test_pull_request_url_is_not_mistaken_for_an_issue(self, repository: Repository) -> None:
        run = publish(
            repository,
            with_documents({"slug": "request", "kind": "primary", "body": "https://github.com/o/r/pull/7\n"}),
        )

        assert run["issue_number"] is None


class TestMigration:
    def test_existing_v1_database_gains_the_columns(self, tmp_path) -> None:
        """An already-deployed database must pick the new columns up."""
        path = tmp_path / "old.sqlite3"
        connection = db.connect(path)
        connection.executescript(db._SCHEMA_V1)
        connection.execute("PRAGMA user_version = 1")
        connection.close()

        upgraded = db.initialise(path)
        columns = {row["name"] for row in upgraded.execute("PRAGMA table_info(runs)").fetchall()}
        version = upgraded.execute("PRAGMA user_version").fetchone()[0]
        upgraded.close()

        assert {"issue_url", "issue_number"} <= columns
        assert version == db.SCHEMA_VERSION

    def test_migration_is_idempotent(self, tmp_path) -> None:
        path = tmp_path / "fresh.sqlite3"
        for _ in range(3):
            connection = db.initialise(path)
            connection.close()

        connection = db.connect(path)
        version = connection.execute("PRAGMA user_version").fetchone()[0]
        connection.close()

        assert version == db.SCHEMA_VERSION


class TestExposure:
    def test_api_reports_the_issue(self, client: TestClient, auth: dict) -> None:
        payload = with_documents({"slug": "request", "kind": "primary", "body": f"{ISSUE_URL}\n"})
        client.post("/api/runs", json=payload, headers=auth)

        run = client.get(f"/api/runs/{RUN_ID}").json()

        assert run["issue_url"] == ISSUE_URL
        assert run["issue_number"] == 110

    def test_run_page_links_the_issue(self, client: TestClient, auth: dict) -> None:
        payload = with_documents({"slug": "request", "kind": "primary", "body": f"{ISSUE_URL}\n"})
        client.post("/api/runs", json=payload, headers=auth)

        body = client.get(f"/run/{RUN_ID}").text

        assert f'href="{ISSUE_URL}"' in body
        assert "Issue #110" in body
        assert 'rel="noopener noreferrer"' in body

    def test_list_page_links_the_issue(self, client: TestClient, auth: dict) -> None:
        payload = with_documents({"slug": "request", "kind": "primary", "body": f"{ISSUE_URL}\n"})
        client.post("/api/runs", json=payload, headers=auth)

        assert f'href="{ISSUE_URL}"' in client.get("/").text

    def test_unlinked_number_is_shown_without_an_anchor(self, client: TestClient, auth: dict) -> None:
        payload = with_documents(
            {"slug": "request", "kind": "primary", "body": "# Request: #110 grass\n"}
        )
        client.post("/api/runs", json=payload, headers=auth)

        body = client.get(f"/run/{RUN_ID}").text

        assert "Issue #110" in body
        assert "issue-link is-plain" in body

    def test_no_issue_renders_nothing(self, client: TestClient, auth: dict) -> None:
        payload = with_documents({"slug": "request", "kind": "primary", "body": "nothing here\n"})
        client.post("/api/runs", json=payload, headers=auth)

        assert "issue-link" not in client.get(f"/run/{RUN_ID}").text

    def test_template_applies_end_to_end(self, settings: Settings, auth: dict) -> None:
        configured = dataclasses.replace(
            settings, issue_linking=IssueLinking(default_template=TEMPLATE)
        )
        payload = with_documents(
            {"slug": "request", "kind": "primary", "body": "# Request: #116 blocks (r6)\n"}
        )

        with TestClient(create_app(configured)) as client:
            client.post("/api/runs", json=payload, headers=auth)
            body = client.get(f"/run/{RUN_ID}").text

        assert "https://github.com/daniell0gda/poke-defense-godot/issues/116" in body
