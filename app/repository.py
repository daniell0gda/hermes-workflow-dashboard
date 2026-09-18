"""All SQL lives here; nothing above this layer writes a query."""

from __future__ import annotations

import re
import sqlite3
from contextlib import contextmanager
from typing import Any, Iterable, Iterator, Mapping, Sequence

from . import formatting

RUN_STATUSES = ("running", "completed", "failed", "cancelled", "interrupted", "unknown")

# Abandoned is derived from heartbeat age rather than stored, so it is a filter
# value and never a status a worker may publish.
STATUS_ABANDONED = "abandoned"

# A run still stored as running whose heartbeat has expired. A run that never
# sent one is not called abandoned, matching formatting.health.
ABANDONED_SQL = "status = 'running' AND heartbeat_at IS NOT NULL AND heartbeat_at < ?"
LIVE_SQL = "status = 'running' AND (heartbeat_at IS NULL OR heartbeat_at >= ?)"

PER_PAGE_MAX = 200
PER_PAGE_DEFAULT = 50

# Guards the dynamic UPDATE/INSERT in save_run against a stray key.
RUN_COLUMNS = frozenset(
    {
        "feature",
        "status",
        "classification",
        "phase",
        "active_node",
        "last_node",
        "host",
        "started_at",
        "ended_at",
        "heartbeat_at",
        "duration_ms",
        "worker_ms",
        "event_count",
        "revisions",
        "cost_usd",
        "input_tokens",
        "output_tokens",
        "error",
        "graph",
        "metrics",
        "issue_url",
        "issue_number",
        "project",
        "gh_status",
        "done",
    }
)

# A done filter value to its SQL. Anything else filters nothing, matching how
# an unrecognised status filter behaves.
DONE_FILTERS = {"yes": "done = 1", "no": "done = 0"}

# A run nobody has reported an issue state for is neither open nor closed.
ISSUE_REPORTED_SQL = "gh_status IS NOT NULL AND gh_status != ''"

# When a run counts as having happened: what it cost is spent as it starts, and
# a run that never stated a start is placed where the list already orders it.
RUN_MOMENT_SQL = "COALESCE(started_at, updated_at)"


def _rows(cursor: sqlite3.Cursor) -> list[dict[str, Any]]:
    return [dict(row) for row in cursor.fetchall()]


def _optional_float(value: Any) -> float | None:
    return None if value is None else float(value)


def _status_condition(status: str) -> tuple[str, list[Any]] | None:
    """SQL for a status filter, or None when the filter is not recognised."""
    if status == STATUS_ABANDONED:
        return ABANDONED_SQL, [formatting.abandoned_cutoff()]
    # Running means still alive: an expired run answers to 'abandoned' instead.
    if status == "running":
        return LIVE_SQL, [formatting.abandoned_cutoff()]
    if status in RUN_STATUSES:
        return "status = ?", [status]
    if status == "other":
        return "status NOT IN ('running', 'completed', 'failed')", []

    return None


class Repository:
    def __init__(self, connection: sqlite3.Connection) -> None:
        self._connection = connection

    @contextmanager
    def transaction(self) -> Iterator[None]:
        """Make a publish all-or-nothing.

        The connection runs in autocommit mode, so transactions are explicit.
        BEGIN IMMEDIATE takes the write lock up front rather than failing
        halfway through a multi-table publish.
        """
        self._connection.execute("BEGIN IMMEDIATE")
        try:
            yield
        except BaseException:
            self._connection.execute("ROLLBACK")
            raise
        else:
            self._connection.execute("COMMIT")

    # ---------------------------------------------------------------- runs

    def find_run(self, run_id: str) -> dict[str, Any] | None:
        row = self._connection.execute("SELECT * FROM runs WHERE run_id = ?", (run_id,)).fetchone()

        return dict(row) if row else None

    def run_exists(self, run_id: str) -> bool:
        found = self._connection.execute("SELECT 1 FROM runs WHERE run_id = ?", (run_id,)).fetchone()

        return found is not None

    def save_run(self, run_id: str, fields: Mapping[str, Any]) -> None:
        """Upsert, writing only the given columns.

        A status patch must not clobber the columns a full publish derived, so
        absent keys are left untouched rather than defaulted.
        """
        unknown = set(fields) - RUN_COLUMNS
        if unknown:
            raise ValueError(f"unknown run columns: {sorted(unknown)}")

        payload: dict[str, Any] = dict(fields)
        payload["updated_at"] = formatting.now_text()

        if self.run_exists(run_id):
            assignments = ", ".join(f"{column} = ?" for column in payload)
            self._connection.execute(
                f"UPDATE runs SET {assignments} WHERE run_id = ?",
                [*payload.values(), run_id],
            )
            return

        payload["run_id"] = run_id
        payload["created_at"] = payload["updated_at"]
        columns = ", ".join(payload)
        placeholders = ", ".join("?" * len(payload))
        self._connection.execute(
            f"INSERT INTO runs ({columns}) VALUES ({placeholders})",
            list(payload.values()),
        )

    def touch_heartbeat(self, run_id: str) -> bool:
        now = formatting.now_text()
        cursor = self._connection.execute(
            "UPDATE runs SET heartbeat_at = ?, updated_at = ? WHERE run_id = ?",
            (now, now, run_id),
        )

        return cursor.rowcount > 0

    def delete_run(self, run_id: str) -> None:
        # Child rows go with it via ON DELETE CASCADE.
        self._connection.execute("DELETE FROM runs WHERE run_id = ?", (run_id,))

    def query_runs(
        self,
        status: str = "",
        feature: str = "",
        search: str = "",
        page: int = 1,
        per_page: int = PER_PAGE_DEFAULT,
        project: str = "",
        done: str = "",
    ) -> dict[str, Any]:
        per_page = min(PER_PAGE_MAX, max(1, int(per_page)))
        page = max(1, int(page))

        clauses: list[str] = []
        params: list[Any] = []

        condition = _status_condition(status)
        if condition is not None:
            clauses.append(f"({condition[0]})")
            params.extend(condition[1])

        if feature:
            clauses.append("feature = ?")
            params.append(feature)

        if project:
            clauses.append("project = ?")
            params.append(project)

        if done in DONE_FILTERS:
            clauses.append(DONE_FILTERS[done])

        if search.strip():
            like = f"%{search.strip()}%"
            clauses.append("(run_id LIKE ? OR feature LIKE ? OR project LIKE ?)")
            params.extend([like, like, like])

        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""

        total = self._connection.execute(
            f"SELECT COUNT(*) FROM runs {where}", params
        ).fetchone()[0]

        rows = _rows(
            self._connection.execute(
                f"SELECT * FROM runs {where} "
                f"ORDER BY {RUN_MOMENT_SQL} DESC, rowid DESC "
                "LIMIT ? OFFSET ?",
                [*params, per_page, (page - 1) * per_page],
            )
        )

        return {
            "rows": rows,
            "total": total,
            "page": page,
            "per_page": per_page,
            "pages": max(1, -(-total // per_page)),
        }

    def projects(self) -> list[str]:
        """Distinct projects that have runs, for filtering."""
        return [
            row["project"]
            for row in self._connection.execute(
                "SELECT project FROM runs WHERE project IS NOT NULL AND project != '' "
                "GROUP BY project ORDER BY COUNT(*) DESC, project ASC"
            ).fetchall()
        ]

    def active_runs(self, limit: int = 10) -> list[dict[str, Any]]:
        """Live runs only, so an abandoned one stops claiming to be running now."""
        return _rows(
            self._connection.execute(
                f"SELECT * FROM runs WHERE {LIVE_SQL} "
                f"ORDER BY {RUN_MOMENT_SQL} DESC LIMIT ?",
                (formatting.abandoned_cutoff(), limit),
            )
        )

    def summary(self) -> dict[str, Any]:
        cutoff = formatting.abandoned_cutoff()
        row = self._connection.execute(
            f"""
            SELECT COUNT(*)                          AS total,
                   SUM({LIVE_SQL})                   AS running,
                   SUM({ABANDONED_SQL})              AS abandoned,
                   SUM(status = 'completed')         AS completed,
                   SUM(status = 'failed')            AS failed,
                   SUM(classification = 'pass')      AS passed,
                   SUM(done)                         AS done,
                   SUM({ISSUE_REPORTED_SQL})         AS issue_reported,
                   SUM(revisions)                    AS revisions,
                   SUM(event_count)                  AS worker_calls,
                   AVG(NULLIF(duration_ms, 0))       AS avg_duration_ms,
                   SUM(cost_usd)                     AS cost_usd,
                   SUM(input_tokens)                 AS input_tokens,
                   SUM(output_tokens)                AS output_tokens
            FROM runs
            """,
            (cutoff, cutoff),
        ).fetchone()

        def integer(key: str) -> int:
            return int(row[key] or 0)

        def optional_int(key: str) -> int | None:
            return None if row[key] is None else int(row[key])

        return {
            "total": integer("total"),
            "running": integer("running"),
            "abandoned": integer("abandoned"),
            "completed": integer("completed"),
            "failed": integer("failed"),
            "passed": integer("passed"),
            "done": integer("done"),
            "issue_reported": integer("issue_reported"),
            "revisions": integer("revisions"),
            "worker_calls": integer("worker_calls"),
            "avg_duration_ms": None if row["avg_duration_ms"] is None else round(row["avg_duration_ms"]),
            "cost_usd": _optional_float(row["cost_usd"]),
            "spend": self.spend(),
            "input_tokens": optional_int("input_tokens"),
            "output_tokens": optional_int("output_tokens"),
        }

    def spend(self) -> dict[str, float | None]:
        """What the runs have cost today, this week and this month.

        A period holding no priced run at all sums to NULL and is reported as
        no data rather than as $0.00, exactly as the all-time total is.
        """
        starts = formatting.period_starts()
        row = self._connection.execute(
            f"""
            SELECT SUM(CASE WHEN {RUN_MOMENT_SQL} >= ? THEN cost_usd END) AS today,
                   SUM(CASE WHEN {RUN_MOMENT_SQL} >= ? THEN cost_usd END) AS week,
                   SUM(CASE WHEN {RUN_MOMENT_SQL} >= ? THEN cost_usd END) AS month
            FROM runs
            """,
            (starts["today"], starts["week"], starts["month"]),
        ).fetchone()

        return {period: _optional_float(row[period]) for period in ("today", "week", "month")}

    # -------------------------------------------------------------- events

    def replace_events(self, run_id: str, rows: Iterable[Mapping[str, Any]]) -> list[dict[str, Any]]:
        """The worker republishes the whole timeline, so replace it wholesale."""
        self._connection.execute("DELETE FROM events WHERE run_id = ?", (run_id,))

        written: list[dict[str, Any]] = []
        for row in rows:
            record = {"run_id": run_id, **row}
            self._connection.execute(
                "INSERT INTO events (run_id, seq, node, status, duration_ms, occurred_at, summary, error) "
                "VALUES (:run_id, :seq, :node, :status, :duration_ms, :occurred_at, :summary, :error)",
                record,
            )
            written.append(record)

        return written

    def events_for(self, run_id: str) -> list[dict[str, Any]]:
        return _rows(
            self._connection.execute(
                "SELECT * FROM events WHERE run_id = ? ORDER BY seq ASC", (run_id,)
            )
        )

    # ----------------------------------------------------------- documents

    def replace_documents(self, run_id: str, documents: Sequence[Mapping[str, Any]]) -> int:
        self._connection.execute("DELETE FROM documents WHERE run_id = ?", (run_id,))

        for document in documents:
            self._connection.execute(
                "INSERT INTO documents (run_id, slug, title, kind, sort_order, body) "
                "VALUES (:run_id, :slug, :title, :kind, :sort_order, :body)",
                {"run_id": run_id, **document},
            )

        return len(documents)

    def documents_for(self, run_id: str) -> list[dict[str, Any]]:
        return _rows(
            self._connection.execute(
                "SELECT * FROM documents WHERE run_id = ? ORDER BY sort_order ASC, slug ASC",
                (run_id,),
            )
        )

    # ----------------------------------------------------------- artifacts

    def artifacts_for(self, run_id: str) -> list[dict[str, Any]]:
        return _rows(
            self._connection.execute(
                "SELECT * FROM artifacts WHERE run_id = ? ORDER BY sort_order ASC, rel_path ASC",
                (run_id,),
            )
        )

    def artifact_manifest(self, run_id: str) -> dict[str, str]:
        """path -> checksum, so a publisher uploads only what changed."""
        return {
            row["rel_path"]: row["checksum"]
            for row in self._connection.execute(
                "SELECT rel_path, checksum FROM artifacts WHERE run_id = ?", (run_id,)
            ).fetchall()
        }

    def save_artifact(self, record: Mapping[str, Any]) -> None:
        self._connection.execute(
            "INSERT INTO artifacts "
            "(run_id, rel_path, file_name, mime, bytes, width, height, animated, checksum, "
            " thumb_name, sort_order, created_at) "
            "VALUES (:run_id, :rel_path, :file_name, :mime, :bytes, :width, :height, :animated, "
            "        :checksum, :thumb_name, :sort_order, :created_at) "
            "ON CONFLICT(run_id, rel_path) DO UPDATE SET "
            "  file_name = excluded.file_name, mime = excluded.mime, bytes = excluded.bytes, "
            "  width = excluded.width, height = excluded.height, animated = excluded.animated, "
            "  checksum = excluded.checksum, thumb_name = excluded.thumb_name",
            record,
        )

    def renumber_artifacts(self, run_id: str) -> None:
        """Keep gallery order natural (01_, 02_, ... 10_) after every upload."""
        paths = [row["rel_path"] for row in self.artifacts_for(run_id)]
        for position, rel_path in enumerate(sorted(paths, key=_natural_key)):
            self._connection.execute(
                "UPDATE artifacts SET sort_order = ? WHERE run_id = ? AND rel_path = ?",
                (position, run_id, rel_path),
            )


def _natural_key(text: str) -> list[tuple[int, Any]]:
    """Sort 10_x after 2_x. Tagged tuples keep int and str parts comparable."""
    return [
        (1, int(part)) if part.isdigit() else (0, part)
        for part in re.split(r"(\d+)", text)
        if part
    ]
