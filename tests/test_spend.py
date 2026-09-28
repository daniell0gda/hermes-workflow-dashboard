"""Spend by period: what the runs cost today, this week and this month."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from fastapi.testclient import TestClient

from app import formatting
from tests.conftest import sample_payload

# Mid-afternoon on a Thursday, so every boundary the tests derive is in the past.
FIXED_NOW = datetime(2026, 8, 27, 15, 30, tzinfo=timezone.utc)


def priced_payload(run_id: str, started: datetime, cost: float) -> dict:
    """A completed run with a known cost that began at a known moment."""
    payload = sample_payload(run_id=run_id)
    payload["status"]["started_at"] = started.isoformat()
    payload["status"]["ended_at"] = (started + timedelta(minutes=5)).isoformat()
    # Stated as a total, so the cost is priced rather than an unknown placeholder.
    payload["metrics"] = {"cost_usd": cost}

    return payload


def publish(client: TestClient, auth: dict, payload: dict) -> None:
    assert client.post("/api/runs", json=payload, headers=auth).status_code == 200


def spend_of(client: TestClient) -> dict:
    return client.get("/api/summary").json()["spend"]


def boundary(period: str) -> datetime:
    return formatting.parse_any(formatting.period_starts()[period])


def before_every_period() -> datetime:
    """A moment older than the earliest boundary, whatever weekday it is now.

    Storage timestamps sort chronologically as text, so the minimum is the
    earliest boundary. Taking it wholesale keeps the test correct in a week that
    straddles the first of the month, where the month starts after the week did.
    """
    earliest = min(formatting.period_starts().values())

    return formatting.parse_any(earliest) - timedelta(minutes=1)


class TestPeriodStarts:
    def test_every_period_starts_at_a_local_midnight(self) -> None:
        for value in formatting.period_starts(FIXED_NOW).values():
            local = formatting.parse_any(value).astimezone()

            assert (local.hour, local.minute, local.second) == (0, 0, 0)

    def test_the_week_starts_on_a_monday(self) -> None:
        week = formatting.parse_any(formatting.period_starts(FIXED_NOW)["week"])

        assert week.astimezone().weekday() == 0

    def test_the_month_starts_on_the_first(self) -> None:
        month = formatting.parse_any(formatting.period_starts(FIXED_NOW)["month"])

        assert month.astimezone().day == 1

    def test_no_period_starts_in_the_future(self) -> None:
        for value in formatting.period_starts(FIXED_NOW).values():
            assert formatting.parse_any(value) <= FIXED_NOW


class TestSpendSummary:
    def test_a_run_without_a_priced_cost_reports_no_data(
        self, client: TestClient, auth: dict
    ) -> None:
        # The sample run's invocations all carry cost_status: unknown.
        publish(client, auth, sample_payload())

        assert spend_of(client) == {"today": None, "week": None, "month": None}

    def test_a_run_from_today_counts_in_every_period(self, client: TestClient, auth: dict) -> None:
        publish(client, auth, priced_payload("today-run", formatting.utc_now(), 1.25))

        assert spend_of(client) == {"today": 1.25, "week": 1.25, "month": 1.25}

    def test_a_run_from_before_today_is_left_out_of_today(
        self, client: TestClient, auth: dict
    ) -> None:
        publish(client, auth, priced_payload("older-run", boundary("today") - timedelta(minutes=1), 3.0))

        assert spend_of(client)["today"] is None
        assert client.get("/api/summary").json()["cost_usd"] == 3.0

    def test_a_run_starting_on_the_boundary_belongs_to_the_period(
        self, client: TestClient, auth: dict
    ) -> None:
        publish(client, auth, priced_payload("midnight-run", boundary("today"), 2.0))

        assert spend_of(client)["today"] == 2.0

    def test_older_spend_stays_in_the_all_time_total_only(
        self, client: TestClient, auth: dict
    ) -> None:
        publish(client, auth, priced_payload("ancient-run", before_every_period(), 4.0))
        publish(client, auth, priced_payload("today-run", formatting.utc_now(), 1.0))

        assert spend_of(client) == {"today": 1.0, "week": 1.0, "month": 1.0}
        assert client.get("/api/summary").json()["cost_usd"] == 5.0

    def test_a_run_without_a_start_falls_back_to_when_it_was_published(
        self, client: TestClient, auth: dict
    ) -> None:
        payload = priced_payload("startless-run", formatting.utc_now(), 0.5)
        payload["status"]["started_at"] = None

        publish(client, auth, payload)

        assert spend_of(client)["today"] == 0.5


class TestListView:
    def test_the_spend_tiles_appear_once_a_run_is_priced(
        self, client: TestClient, auth: dict
    ) -> None:
        publish(client, auth, priced_payload("today-run", formatting.utc_now(), 1.25))

        body = client.get("/").text

        for label in ("Spent today", "Spent this week", "Spent this month", "Total cost"):
            assert label in body
        assert "$1.25" in body

    def test_no_spend_tiles_without_a_priced_run(self, client: TestClient, auth: dict) -> None:
        publish(client, auth, sample_payload())

        assert "Spent today" not in client.get("/").text
