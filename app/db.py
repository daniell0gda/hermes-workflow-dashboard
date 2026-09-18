"""SQLite access and schema migrations.

WAL mode lets the worker publish while somebody is reading a run page. All
timestamps are stored as ``YYYY-MM-DD HH:MM:SS`` in UTC, which sorts correctly
as text and needs no timezone handling in SQL.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

SCHEMA_VERSION = 4

_SCHEMA_V1 = """
CREATE TABLE runs (
    run_id         TEXT PRIMARY KEY,
    feature        TEXT,
    status         TEXT NOT NULL DEFAULT 'unknown',
    classification TEXT,
    phase          TEXT,
    active_node    TEXT,
    last_node      TEXT,
    host           TEXT,
    started_at     TEXT,
    ended_at       TEXT,
    heartbeat_at   TEXT,
    duration_ms    INTEGER,
    worker_ms      INTEGER,
    event_count    INTEGER NOT NULL DEFAULT 0,
    revisions      INTEGER NOT NULL DEFAULT 0,
    cost_usd       REAL,
    input_tokens   INTEGER,
    output_tokens  INTEGER,
    error          TEXT,
    graph          TEXT,
    metrics        TEXT,
    created_at     TEXT NOT NULL,
    updated_at     TEXT NOT NULL
);

CREATE INDEX idx_runs_status  ON runs(status, updated_at DESC);
CREATE INDEX idx_runs_started ON runs(started_at DESC);
CREATE INDEX idx_runs_feature ON runs(feature);

CREATE TABLE events (
    run_id      TEXT NOT NULL REFERENCES runs(run_id) ON DELETE CASCADE,
    seq         INTEGER NOT NULL,
    node        TEXT,
    status      TEXT,
    duration_ms INTEGER,
    occurred_at TEXT,
    summary     TEXT,
    error       TEXT,
    PRIMARY KEY (run_id, seq)
);

CREATE TABLE documents (
    run_id     TEXT NOT NULL REFERENCES runs(run_id) ON DELETE CASCADE,
    slug       TEXT NOT NULL,
    title      TEXT NOT NULL,
    kind       TEXT NOT NULL DEFAULT 'primary',
    sort_order INTEGER NOT NULL DEFAULT 0,
    body       TEXT NOT NULL,
    PRIMARY KEY (run_id, slug)
);

CREATE INDEX idx_documents_kind ON documents(run_id, kind, sort_order);

CREATE TABLE artifacts (
    run_id     TEXT NOT NULL REFERENCES runs(run_id) ON DELETE CASCADE,
    rel_path   TEXT NOT NULL,
    file_name  TEXT NOT NULL,
    mime       TEXT NOT NULL,
    bytes      INTEGER NOT NULL DEFAULT 0,
    width      INTEGER,
    height     INTEGER,
    animated   INTEGER NOT NULL DEFAULT 0,
    checksum   TEXT NOT NULL DEFAULT '',
    thumb_name TEXT,
    sort_order INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    PRIMARY KEY (run_id, rel_path)
);

CREATE INDEX idx_artifacts_order ON artifacts(run_id, sort_order);
"""

# Link back to the issue the run came from.
_SCHEMA_V2 = """
ALTER TABLE runs ADD COLUMN issue_url TEXT;
ALTER TABLE runs ADD COLUMN issue_number INTEGER;
"""

# Which project (repository) the run belongs to.
_SCHEMA_V3 = """
ALTER TABLE runs ADD COLUMN project TEXT;
CREATE INDEX idx_runs_project ON runs(project);
"""

# State of that issue on the forge, and whether it counts as resolved.
_SCHEMA_V4 = """
ALTER TABLE runs ADD COLUMN gh_status TEXT;
ALTER TABLE runs ADD COLUMN done INTEGER NOT NULL DEFAULT 0;
CREATE INDEX idx_runs_done ON runs(done);
"""

# Index i brings the database from version i to version i + 1.
_MIGRATIONS: tuple[str, ...] = (_SCHEMA_V1, _SCHEMA_V2, _SCHEMA_V3, _SCHEMA_V4)


def connect(database_path: Path) -> sqlite3.Connection:
    database_path.parent.mkdir(parents=True, exist_ok=True)
    # check_same_thread=False because a request's dependency and its handler can
    # land on different worker threads (and the media upload hands the write to
    # the thread pool). Each request gets its own connection and never uses it
    # from two threads at once, which is what the check actually guards against.
    connection = sqlite3.connect(
        database_path,
        timeout=15.0,
        isolation_level=None,
        check_same_thread=False,
    )
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA journal_mode = WAL")
    connection.execute("PRAGMA synchronous = NORMAL")
    connection.execute("PRAGMA foreign_keys = ON")
    connection.execute("PRAGMA busy_timeout = 10000")

    return connection


def migrate(connection: sqlite3.Connection) -> None:
    """Apply every migration the database has not seen yet.

    A fresh file runs them all in order; an existing one picks up where its
    user_version left off.
    """
    version = connection.execute("PRAGMA user_version").fetchone()[0]
    if version >= SCHEMA_VERSION:
        return

    for target, script in enumerate(_MIGRATIONS, start=1):
        if version < target:
            connection.executescript(script)

    connection.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")


def initialise(database_path: Path) -> sqlite3.Connection:
    connection = connect(database_path)
    migrate(connection)

    return connection
