from __future__ import annotations

import pytest

from app import ingest
from app.repository import Repository
from tests.conftest import sample_payload


def publish(repository: Repository, payload: dict) -> dict:
    run_id = ingest.sanitize_run_id(payload["run_id"])
    with repository.transaction():
        ingest.publish(repository, run_id, payload)

    return repository.find_run(run_id)


class TestRunIdSanitising:
    @pytest.mark.parametrize("value", ["", ".", "..", "///", "?", "x" * 129])
    def test_rejects_unusable(self, value: str) -> None:
        with pytest.raises(ingest.IngestError):
            ingest.sanitize_run_id(value)

    @pytest.mark.parametrize(
        ("given", "expected"),
        [
            ("issue116-game-ready-r6", "issue116-game-ready-r6"),
            ("../../etc/passwd", "....etcpasswd"),
            ("run id with spaces", "runidwithspaces"),
        ],
    )
    def test_strips_unsafe_characters(self, given: str, expected: str) -> None:
        assert ingest.sanitize_run_id(given) == expected


class TestSlugSanitising:
    @pytest.mark.parametrize(
        ("given", "expected"),
        [
            ("report.md", "report"),
            ("clusters/1-thing.md", "clusters/1-thing"),
            ("../../../etc/passwd", "etc/passwd"),
            ("a/b/c/d.md", "a/b"),
        ],
    )
    def test_keeps_two_safe_segments(self, given: str, expected: str) -> None:
        assert ingest.sanitize_slug(given) == expected


class TestDerivedFields:
    def test_duration_from_start_and_end(self, repository: Repository) -> None:
        run = publish(repository, sample_payload())

        assert run["duration_ms"] == 20 * 60 * 1000

    def test_worker_time_sums_event_durations(self, repository: Repository) -> None:
        run = publish(repository, sample_payload())

        assert run["worker_ms"] == 300000 + 600000 + 120000 + 90000 + 30000

    def test_revisions_count_extra_code_passes(self, repository: Repository) -> None:
        run = publish(repository, sample_payload())

        assert run["event_count"] == 5
        assert run["revisions"] == 1

    def test_single_code_pass_is_zero_revisions(self, repository: Repository) -> None:
        payload = sample_payload()
        payload["events"] = [event for event in payload["events"] if event["summary"] != "Revision 1."]

        assert publish(repository, payload)["revisions"] == 0

    def test_verdict_from_team_leader_report(self, repository: Repository) -> None:
        assert publish(repository, sample_payload())["classification"] == "pass"

    def test_verdict_falls_back_to_check_report(self, repository: Repository) -> None:
        payload = sample_payload()
        payload["documents"] = [
            {"slug": "check", "kind": "primary", "body": "# Check\n\nclassification: fixable\n"}
        ]

        assert publish(repository, payload)["classification"] == "fixable"

    def test_verdict_absent_when_no_report_states_one(self, repository: Repository) -> None:
        payload = sample_payload()
        payload["documents"] = [{"slug": "plan", "kind": "primary", "body": "# Plan\n"}]

        assert publish(repository, payload)["classification"] is None

    def test_last_node_derived_from_final_event(self, repository: Repository) -> None:
        assert publish(repository, sample_payload())["last_node"] == "team-leader"

    def test_unknown_status_is_normalised(self, repository: Repository) -> None:
        payload = sample_payload()
        payload["status"]["status"] = "something-else"

        assert publish(repository, payload)["status"] == "unknown"


class TestUsage:
    def test_tokens_summed_from_invocations(self, repository: Repository) -> None:
        run = publish(repository, sample_payload())

        assert run["input_tokens"] == 3000
        assert run["output_tokens"] == 350

    def test_cost_withheld_while_every_invocation_is_unknown(self, repository: Repository) -> None:
        """A cost_status of unknown makes the 0.0 a placeholder, not a real $0."""
        assert publish(repository, sample_payload())["cost_usd"] is None

    def test_cost_reported_once_pricing_resolves(self, repository: Repository) -> None:
        payload = sample_payload()
        payload["metrics"]["invocations"][0].update({"cost_usd": 0.25, "cost_status": "resolved"})
        payload["metrics"]["invocations"][1].update({"cost_usd": 0.75, "cost_status": "resolved"})

        assert publish(repository, payload)["cost_usd"] == pytest.approx(1.0)

    def test_explicit_totals_win_over_invocations(self, repository: Repository) -> None:
        payload = sample_payload()
        payload["metrics"]["input_tokens"] = 99
        payload["metrics"]["output_tokens"] = 9

        run = publish(repository, payload)

        assert (run["input_tokens"], run["output_tokens"]) == (99, 9)

    def test_empty_metrics_leave_usage_blank(self, repository: Repository) -> None:
        payload = sample_payload()
        payload["metrics"] = {}

        run = publish(repository, payload)

        assert (run["cost_usd"], run["input_tokens"], run["output_tokens"]) == (None, None, None)


class TestDocuments:
    def test_titles_and_order_are_assigned(self, repository: Repository) -> None:
        run_id = ingest.sanitize_run_id("demo-run-r1")
        publish(repository, sample_payload())

        documents = repository.documents_for(run_id)
        titles = [document["title"] for document in documents]

        assert titles[:2] == ["Team-leader report", "Plan"]
        # Cluster titles come from the slug, with the leading index stripped.
        assert (documents[-1]["title"], documents[-1]["kind"]) == ("First", "cluster")

    def test_blank_bodies_are_dropped(self, repository: Repository) -> None:
        payload = sample_payload()
        payload["documents"].append({"slug": "report", "kind": "primary", "body": "   \n"})
        publish(repository, payload)

        slugs = {document["slug"] for document in repository.documents_for("demo-run-r1")}

        assert "report" not in slugs

    def test_republish_replaces_rather_than_duplicates(self, repository: Repository) -> None:
        publish(repository, sample_payload())
        publish(repository, sample_payload())

        assert len(repository.documents_for("demo-run-r1")) == 3
        assert len(repository.events_for("demo-run-r1")) == 5


class TestPatch:
    def test_finishing_a_run_sets_end_and_duration(self, repository: Repository) -> None:
        payload = sample_payload()
        payload["status"].update({"status": "running", "ended_at": None})
        publish(repository, payload)

        with repository.transaction():
            run = ingest.patch(repository, "demo-run-r1", {"status": "completed"})

        assert run["status"] == "completed"
        assert run["ended_at"] is not None
        assert run["duration_ms"] is not None

    def test_patch_does_not_clobber_derived_columns(self, repository: Repository) -> None:
        publish(repository, sample_payload())

        with repository.transaction():
            run = ingest.patch(repository, "demo-run-r1", {"phase": "check"})

        assert run["phase"] == "check"
        assert run["revisions"] == 1
        assert run["event_count"] == 5
        assert run["input_tokens"] == 3000

    def test_unknown_run_raises(self, repository: Repository) -> None:
        with pytest.raises(LookupError):
            ingest.patch(repository, "nope", {"status": "failed"})
