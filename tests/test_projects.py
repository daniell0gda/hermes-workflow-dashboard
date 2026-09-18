"""Runs from more than one project."""

from __future__ import annotations

import dataclasses

import pytest
from fastapi.testclient import TestClient

from app import ingest
from app.config import IssueLinking, Settings
from app.main import create_app
from app.repository import Repository
from tests.conftest import sample_payload

GODOT_URL = "https://github.com/daniell0gda/poke-defense-godot/issues/114"
PIWOT_URL = "https://github.com/daniell0gda/piwotworki/issues/7"
GITEA_URL = "https://gitea.lan/dan/piwotworki/issues/7"

OWNER_TEMPLATE = "https://github.com/daniell0gda/{project}/issues/{number}"


def run_payload(run_id: str, body: str, **extra) -> dict:
    payload = sample_payload(run_id=run_id)
    payload["documents"] = [{"slug": "request", "kind": "primary", "body": body}]
    payload.update(extra)

    return payload


def publish(repository: Repository, payload: dict, linking: IssueLinking | None = None) -> dict:
    run_id = ingest.sanitize_run_id(payload["run_id"])
    with repository.transaction():
        ingest.publish(repository, run_id, payload, linking)

    return repository.find_run(run_id)


class TestProjectFromIssueUrl:
    @pytest.mark.parametrize(
        ("url", "expected"),
        [
            (GODOT_URL, "poke-defense-godot"),
            (PIWOT_URL, "piwotworki"),
            (GITEA_URL, "piwotworki"),
            ("https://gitlab.com/dan/piwotworki/-/issues/7", "piwotworki"),
        ],
    )
    def test_repository_name_is_taken_from_the_url(
        self, repository: Repository, url: str, expected: str
    ) -> None:
        """No configuration needed: the URL names its own repository."""
        run = publish(repository, run_payload("r1", f"Issue: {url}\n"))

        assert run["project"] == expected

    def test_two_projects_resolve_independently(self, repository: Repository) -> None:
        first = publish(repository, run_payload("godot-run", f"{GODOT_URL}\n"))
        second = publish(repository, run_payload("piwot-run", f"{PIWOT_URL}\n"))

        assert (first["project"], first["issue_number"]) == ("poke-defense-godot", 114)
        assert (second["project"], second["issue_number"]) == ("piwotworki", 7)

    def test_payload_project_overrides_the_url(self, repository: Repository) -> None:
        run = publish(repository, run_payload("r1", f"{GODOT_URL}\n", project="renamed-repo"))

        assert run["project"] == "renamed-repo"

    def test_workspace_paths_are_ignored(self, repository: Repository) -> None:
        """`godot-td` is the runner workspace, not the repository - measurably so
        on 12 published runs where the two disagree."""
        body = (
            "# Request: issue #114\n"
            "- Project: godot-td\n"
            "- Workspace: /workspace/git-workspaces/godot-td/issue-x\n"
            f"- Issue: {GODOT_URL}\n"
        )

        assert publish(repository, run_payload("r1", body))["project"] == "poke-defense-godot"

    def test_no_project_without_a_url_or_payload_field(self, repository: Repository) -> None:
        run = publish(repository, run_payload("r1", "# Request: #114 something\n"))

        assert run["project"] is None


class TestProjectPlaceholderTemplate:
    def test_one_template_serves_every_project(self, repository: Repository) -> None:
        linking = IssueLinking(default_template=OWNER_TEMPLATE)
        body = "# Request: #7 thing\n"

        run = publish(repository, run_payload("r1", body, project="piwotworki"), linking)

        assert run["issue_url"] == "https://github.com/daniell0gda/piwotworki/issues/7"

    def test_unknown_project_yields_no_link_rather_than_a_wrong_one(
        self, repository: Repository
    ) -> None:
        linking = IssueLinking(default_template=OWNER_TEMPLATE)

        run = publish(repository, run_payload("r1", "# Request: #7 thing\n"), linking)

        assert run["issue_number"] == 7
        assert run["issue_url"] is None

    def test_per_project_template_beats_the_placeholder_default(self, repository: Repository) -> None:
        linking = IssueLinking(
            default_template=OWNER_TEMPLATE,
            per_project={"piwotworki": "https://gitea.lan/dan/piwotworki/issues/{number}"},
        )

        run = publish(repository, run_payload("r1", "# Request: #7 x\n", project="piwotworki"), linking)

        assert run["issue_url"] == "https://gitea.lan/dan/piwotworki/issues/7"

    def test_project_outside_the_map_uses_the_default(self, repository: Repository) -> None:
        linking = IssueLinking(
            default_template=OWNER_TEMPLATE,
            per_project={"piwotworki": "https://gitea.lan/dan/piwotworki/issues/{number}"},
        )

        run = publish(
            repository, run_payload("r1", "# Request: #9 x\n", project="poke-defense-godot"), linking
        )

        assert run["issue_url"] == "https://github.com/daniell0gda/poke-defense-godot/issues/9"


class TestListingAndFiltering:
    def seed(self, client: TestClient, auth: dict) -> None:
        for run_id, url in (
            ("godot-a", GODOT_URL),
            ("godot-b", GODOT_URL),
            ("piwot-a", PIWOT_URL),
        ):
            response = client.post("/api/runs", json=run_payload(run_id, f"{url}\n"), headers=auth)
            assert response.status_code == 200

    def test_api_filters_by_project(self, client: TestClient, auth: dict) -> None:
        self.seed(client, auth)

        body = client.get("/api/runs", params={"project": "piwotworki"}).json()

        assert [run["run_id"] for run in body["runs"]] == ["piwot-a"]

    def test_api_exposes_the_project(self, client: TestClient, auth: dict) -> None:
        self.seed(client, auth)

        assert client.get("/api/runs/godot-a").json()["project"] == "poke-defense-godot"

    def test_search_matches_the_project(self, client: TestClient, auth: dict) -> None:
        self.seed(client, auth)

        body = client.get("/api/runs", params={"q": "piwot"}).json()

        assert body["total"] == 1

    def test_project_chips_appear_once_there_are_two(self, client: TestClient, auth: dict) -> None:
        self.seed(client, auth)

        page = client.get("/").text

        assert "chips-project" in page
        assert "All projects" in page
        assert ">piwotworki<" in page

    def test_no_project_chips_for_a_single_project(self, client: TestClient, auth: dict) -> None:
        client.post("/api/runs", json=run_payload("godot-a", f"{GODOT_URL}\n"), headers=auth)

        page = client.get("/").text

        assert "chips-project" not in page
        assert "<th scope=\"col\">Project</th>" not in page

    def test_project_column_appears_with_two_projects(self, client: TestClient, auth: dict) -> None:
        self.seed(client, auth)

        assert "<th scope=\"col\">Project</th>" in client.get("/").text

    def test_filtering_the_list_by_project(self, client: TestClient, auth: dict) -> None:
        self.seed(client, auth)

        page = client.get("/", params={"project": "piwotworki"}).text

        assert "piwot-a" in page
        assert "godot-a" not in page

    def test_run_page_links_back_to_its_project(self, client: TestClient, auth: dict) -> None:
        self.seed(client, auth)

        page = client.get("/run/godot-a").text

        assert "project=poke-defense-godot" in page


class TestEndToEnd:
    def test_two_repositories_link_correctly_with_no_configuration(
        self, settings: Settings, auth: dict
    ) -> None:
        """The case that matters: different repos, nothing configured."""
        with TestClient(create_app(settings)) as client:
            client.post("/api/runs", json=run_payload("godot-a", f"{GODOT_URL}\n"), headers=auth)
            client.post("/api/runs", json=run_payload("piwot-a", f"{PIWOT_URL}\n"), headers=auth)

            godot = client.get("/run/godot-a").text
            piwot = client.get("/run/piwot-a").text

        assert f'href="{GODOT_URL}"' in godot
        assert "Issue #114" in godot
        assert f'href="{PIWOT_URL}"' in piwot
        assert "Issue #7" in piwot

    def test_mixed_hosts_via_the_per_project_map(self, settings: Settings, auth: dict) -> None:
        configured = dataclasses.replace(
            settings,
            issue_linking=IssueLinking(
                per_project={
                    "poke-defense-godot": "https://github.com/daniell0gda/poke-defense-godot/issues/{number}",
                    "piwotworki": "https://gitea.lan/dan/piwotworki/issues/{number}",
                }
            ),
        )

        with TestClient(create_app(configured)) as client:
            client.post(
                "/api/runs",
                json=run_payload("piwot-a", "# Request: #7 x\n", project="piwotworki"),
                headers=auth,
            )
            page = client.get("/run/piwot-a").text

        assert 'href="https://gitea.lan/dan/piwotworki/issues/7"' in page
