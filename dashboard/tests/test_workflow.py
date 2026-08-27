from __future__ import annotations

import json

from app import workflow

GRAPH = json.dumps(
    {
        "nodes": [
            {"id": "starting"},
            {"id": "planning"},
            {"id": "implementing"},
            {"id": "checking"},
            {"id": "reporting"},
        ]
    }
)


def stage_map(stages: list[dict]) -> dict[str, str]:
    return {stage["key"]: stage["state"] for stage in stages}


class TestCanonicalNames:
    def test_graph_gerunds_and_worker_names_agree(self) -> None:
        """graph.json says "implementing" where events say "code"."""
        assert workflow.canonical("implementing") == workflow.canonical("code")
        assert workflow.canonical("planning") == workflow.canonical("plan")
        assert workflow.canonical("checking") == workflow.canonical("check")
        assert workflow.canonical("reporting") == workflow.canonical("team-leader")

    def test_unknown_names_survive_as_their_own_stage(self) -> None:
        assert workflow.canonical("Custom Node!") == "customnode"


class TestStages:
    def test_running_run_marks_active_and_pending(self) -> None:
        run = {
            "status": "running",
            "active_node": "code",
            "phase": "code",
            "started_at": "2026-08-25 10:00:00",
            "graph": GRAPH,
        }
        events = [{"node": "plan", "status": "completed", "duration_ms": 1000}]

        states = stage_map(workflow.stages(run, events))

        assert states == {
            "start": workflow.DONE,
            "plan": workflow.DONE,
            "code": workflow.ACTIVE,
            "check": workflow.PENDING,
            "report": workflow.PENDING,
        }

    def test_finished_run_marks_unreached_stages_skipped(self) -> None:
        run = {
            "status": "failed",
            "active_node": None,
            "phase": "code",
            "started_at": "2026-08-25 10:00:00",
            "graph": GRAPH,
        }
        events = [
            {"node": "plan", "status": "completed", "duration_ms": 1000},
            {"node": "code", "status": "failed", "duration_ms": 2000},
        ]

        states = stage_map(workflow.stages(run, events))

        assert states["code"] == workflow.FAILED
        assert states["check"] == workflow.SKIPPED
        assert states["report"] == workflow.SKIPPED

    def test_stage_recovered_after_a_failed_pass_is_done(self) -> None:
        """The latest pass decides the verdict, not the worst one."""
        run = {"status": "completed", "started_at": "2026-08-25 10:00:00", "graph": GRAPH}
        events = [
            {"node": "code", "status": "failed", "duration_ms": 2000},
            {"node": "code", "status": "completed", "duration_ms": 1000},
        ]

        assert stage_map(workflow.stages(run, events))["code"] == workflow.DONE

    def test_stage_retried_after_a_failed_pass_is_active(self) -> None:
        run = {
            "status": "running",
            "active_node": "code",
            "started_at": "2026-08-25 10:00:00",
            "graph": GRAPH,
        }
        events = [{"node": "code", "status": "failed", "duration_ms": 2000}]

        assert stage_map(workflow.stages(run, events))["code"] == workflow.ACTIVE

    def test_repeated_stage_reports_passes_and_total_duration(self) -> None:
        run = {"status": "completed", "started_at": "2026-08-25 10:00:00", "graph": GRAPH}
        events = [
            {"node": "code", "status": "completed", "duration_ms": 1000},
            {"node": "code", "status": "completed", "duration_ms": 500},
        ]

        code = next(stage for stage in workflow.stages(run, events) if stage["key"] == "code")

        assert (code["passes"], code["duration_ms"]) == (2, 1500)

    def test_stage_order_follows_the_graph(self) -> None:
        run = {"status": "completed", "started_at": "2026-08-25 10:00:00", "graph": GRAPH}
        events = [{"node": "team-leader", "status": "completed", "duration_ms": 1}]

        keys = [stage["key"] for stage in workflow.stages(run, events)]

        assert keys == ["start", "plan", "code", "check", "report"]

    def test_without_a_graph_the_events_define_the_order(self) -> None:
        run = {"status": "completed", "started_at": None, "graph": None}
        events = [
            {"node": "check", "status": "completed", "duration_ms": 1},
            {"node": "plan", "status": "completed", "duration_ms": 1},
        ]

        keys = [stage["key"] for stage in workflow.stages(run, events)]

        assert keys == ["check", "plan"]

    def test_broken_graph_json_is_ignored(self) -> None:
        run = {"status": "completed", "started_at": None, "graph": "{not json"}

        assert workflow.stages(run, [{"node": "plan", "status": "completed"}])[0]["key"] == "plan"

    def test_active_stage_appears_even_with_no_events_yet(self) -> None:
        run = {"status": "running", "active_node": "code", "phase": "code", "started_at": None, "graph": None}

        assert stage_map(workflow.stages(run, []))["code"] == workflow.ACTIVE


class TestPassStates:
    def test_every_pass_keeps_its_own_verdict(self) -> None:
        run = {"status": "completed", "started_at": "2026-08-25 10:00:00", "graph": GRAPH}
        events = [
            {"node": "code", "status": "failed"},
            {"node": "code", "status": "completed"},
        ]

        code = next(stage for stage in workflow.stages(run, events) if stage["key"] == "code")

        assert code["pass_states"] == [workflow.FAILED, workflow.DONE]

    def test_the_pass_now_running_counts_as_one_more(self) -> None:
        """The active pass has not reported an event yet, so nothing else lists it."""
        run = {
            "status": "running",
            "active_node": "code",
            "started_at": "2026-08-25 10:00:00",
            "graph": GRAPH,
        }
        events = [{"node": "code", "status": "completed"}]

        code = next(stage for stage in workflow.stages(run, events) if stage["key"] == "code")

        assert code["pass_states"] == [workflow.DONE, workflow.ACTIVE]

    def test_a_stage_nobody_reached_has_no_passes(self) -> None:
        run = {"status": "completed", "started_at": "2026-08-25 10:00:00", "graph": GRAPH}

        check = next(stage for stage in workflow.stages(run, []) if stage["key"] == "check")

        assert check["pass_states"] == []


class TestActiveSince:
    def test_active_stage_starts_when_the_last_call_finished(self) -> None:
        run = {
            "status": "running",
            "active_node": "check",
            "started_at": "2026-08-25 10:00:00",
            "graph": GRAPH,
        }
        events = [
            {"node": "plan", "status": "completed", "occurred_at": "2026-08-25 10:05:00"},
            {"node": "code", "status": "completed", "occurred_at": "2026-08-25 10:20:00"},
        ]

        stages = {stage["key"]: stage["since"] for stage in workflow.stages(run, events)}

        assert stages["check"] == "2026-08-25 10:20:00"

    def test_before_the_first_call_the_run_start_is_used(self) -> None:
        run = {
            "status": "running",
            "active_node": "plan",
            "started_at": "2026-08-25 10:00:00",
            "graph": GRAPH,
        }

        stages = {stage["key"]: stage["since"] for stage in workflow.stages(run, [])}

        assert stages["plan"] == "2026-08-25 10:00:00"

    def test_only_the_active_stage_carries_a_clock(self) -> None:
        run = {
            "status": "running",
            "active_node": "code",
            "started_at": "2026-08-25 10:00:00",
            "graph": GRAPH,
        }
        events = [{"node": "plan", "status": "completed", "occurred_at": "2026-08-25 10:05:00"}]

        timed = [stage["key"] for stage in workflow.stages(run, events) if stage["since"]]

        assert timed == ["code"]

    def test_a_finished_run_has_no_clock_anywhere(self) -> None:
        run = {"status": "completed", "started_at": "2026-08-25 10:00:00", "graph": GRAPH}
        events = [{"node": "plan", "status": "completed", "occurred_at": "2026-08-25 10:05:00"}]

        assert not [stage for stage in workflow.stages(run, events) if stage["since"]]


class TestProgress:
    def test_counts_settled_stages(self) -> None:
        stages = [
            {"state": workflow.DONE},
            {"state": workflow.FAILED},
            {"state": workflow.ACTIVE},
            {"state": workflow.PENDING},
        ]

        assert workflow.progress(stages) == 50

    def test_empty_pipeline_is_zero(self) -> None:
        assert workflow.progress([]) == 0
