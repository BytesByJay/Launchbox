"""Persistent metadata store for Launchbox.

Records every deployment attempt, which container is currently serving each
application, supervisor restart counters, and per-application database
credentials. SQLite is used because it needs no server process and the whole
store is a single file that can be copied as a backup.
"""

import os
import sqlite3
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from launchbox.config import BASE_DIR

DEFAULT_DB_PATH = os.path.join(BASE_DIR, "state", "launchbox.db")

STATUS_IN_PROGRESS = "in_progress"
STATUS_SUCCESS = "success"
STATUS_FAILED = "failed"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS deployments (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    app_name       TEXT    NOT NULL,
    commit_sha     TEXT,
    image_tag      TEXT,
    container_name TEXT,
    status         TEXT    NOT NULL,
    started_at     TEXT    NOT NULL,
    finished_at    TEXT,
    error          TEXT,
    logs           TEXT,
    config_json    TEXT,
    env_json       TEXT,
    image_id       TEXT
);

CREATE INDEX IF NOT EXISTS idx_deployments_app
    ON deployments (app_name, id DESC);

CREATE TABLE IF NOT EXISTS apps (
    app_name           TEXT PRIMARY KEY,
    current_container  TEXT,
    current_image      TEXT,
    current_deployment INTEGER,
    health_state       TEXT,
    restart_attempts   INTEGER NOT NULL DEFAULT 0,
    marked_failed      INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS databases (
    app_name       TEXT PRIMARY KEY,
    engine         TEXT NOT NULL,
    container_name TEXT NOT NULL,
    volume_name    TEXT NOT NULL,
    db_name        TEXT NOT NULL,
    username       TEXT NOT NULL,
    password       TEXT NOT NULL,
    created_at     TEXT NOT NULL
);
"""

# Columns added to `deployments` after the first release of the schema.
# A database created by an earlier version has the table but not these
# columns, and SQLite has no "ADD COLUMN IF NOT EXISTS", so they are added
# one at a time after checking PRAGMA table_info. Keep _SCHEMA above and this
# map in step: _SCHEMA creates them for a fresh file, this migrates an old one.
_ADDED_DEPLOYMENT_COLUMNS = (
    ("config_json", "TEXT"),
    ("env_json", "TEXT"),
    ("image_id", "TEXT"),
)


_LIKE_ESCAPE_CHAR = "\\"


def _escape_like(value: str) -> str:
    """Neutralise SQL LIKE wildcards in a value used as a literal prefix."""
    return (
        value.replace(_LIKE_ESCAPE_CHAR, _LIKE_ESCAPE_CHAR * 2)
        .replace("%", _LIKE_ESCAPE_CHAR + "%")
        .replace("_", _LIKE_ESCAPE_CHAR + "_")
    )


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class StateStore:
    """Thin, synchronous wrapper over the Launchbox SQLite database."""

    def __init__(self, db_path: str = DEFAULT_DB_PATH):
        self.db_path = db_path
        parent = os.path.dirname(os.path.abspath(db_path))
        if parent:
            os.makedirs(parent, exist_ok=True)
        self._conn = sqlite3.connect(db_path)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA foreign_keys = ON")
        self._conn.executescript(_SCHEMA)
        self._migrate()
        self._conn.commit()

    def _migrate(self) -> None:
        """Bring an existing database file up to the current schema.

        Idempotent by construction: every column is added only if
        ``PRAGMA table_info`` says it is absent, so running this against an
        already-current database is a no-op and running it against a
        pre-migration file preserves every existing row.
        """
        existing = {
            row["name"]
            for row in self._conn.execute("PRAGMA table_info(deployments)")
        }
        for column, declaration in _ADDED_DEPLOYMENT_COLUMNS:
            if column not in existing:
                self._conn.execute(
                    f"ALTER TABLE deployments ADD COLUMN {column} {declaration}"
                )
        self._conn.commit()

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> "StateStore":
        return self

    def __exit__(self, *exc_info) -> None:
        self.close()

    # ---------------------------------------------------------------- helpers

    @staticmethod
    def _row_to_dict(row: Optional[sqlite3.Row]) -> Optional[Dict[str, Any]]:
        return dict(row) if row is not None else None

    def _ensure_app_row(self, app_name: str) -> None:
        self._conn.execute(
            "INSERT OR IGNORE INTO apps (app_name) VALUES (?)", (app_name,)
        )

    # ------------------------------------------------------------ deployments

    def record_start(
        self,
        app_name: str,
        commit_sha: Optional[str],
        config_json: Optional[str] = None,
        env_json: Optional[str] = None,
    ) -> int:
        """Open a deployment row.

        ``config_json`` and ``env_json`` capture the *resolved* configuration
        and environment this deployment ran with. A rollback replays them
        rather than re-reading ``apps/<app>/``, which for a git-pushed
        application does not exist at all. Both are optional so that a caller
        which has not resolved them yet (deploy() opens the row before the
        build, so a failed build is still recorded) can fill them in later
        with record_config.
        """
        cur = self._conn.execute(
            "INSERT INTO deployments (app_name, commit_sha, status, started_at, "
            "config_json, env_json) VALUES (?, ?, ?, ?, ?, ?)",
            (
                app_name, commit_sha, STATUS_IN_PROGRESS, _now(),
                config_json, env_json,
            ),
        )
        self._ensure_app_row(app_name)
        self._conn.commit()
        return int(cur.lastrowid)

    def record_config(
        self,
        deployment_id: int,
        config_json: Optional[str],
        env_json: Optional[str],
    ) -> None:
        """Attach the resolved configuration to an already-open row."""
        self._conn.execute(
            "UPDATE deployments SET config_json = ?, env_json = ? WHERE id = ?",
            (config_json, env_json, deployment_id),
        )
        self._conn.commit()

    def record_success(
        self,
        deployment_id: int,
        container_name: str,
        image_tag: str,
        image_id: Optional[str] = None,
    ) -> None:
        """Close a deployment row as successful and repoint the app at it.

        ``image_id`` is the immutable Docker image ID. ``image_tag`` alone is
        not enough to roll back to: an untagged deploy records
        ``launchbox-<app>:latest``, and every later build moves that tag, so
        resolving it during a rollback can land on a NEWER image than the one
        this deployment actually ran.
        """
        row = self._conn.execute(
            "SELECT app_name FROM deployments WHERE id = ?", (deployment_id,)
        ).fetchone()
        if row is None:
            raise KeyError(f"Unknown deployment id: {deployment_id}")
        app_name = row["app_name"]

        self._conn.execute(
            "UPDATE deployments SET status = ?, container_name = ?, image_tag = ?, "
            "image_id = ?, finished_at = ? WHERE id = ?",
            (
                STATUS_SUCCESS, container_name, image_tag, image_id,
                _now(), deployment_id,
            ),
        )
        self._ensure_app_row(app_name)
        self._conn.execute(
            "UPDATE apps SET current_container = ?, current_image = ?, "
            "current_deployment = ?, health_state = 'healthy' WHERE app_name = ?",
            (container_name, image_tag, deployment_id, app_name),
        )
        self._conn.commit()

    def record_failure(
        self, deployment_id: int, error: str, logs: Optional[str] = None
    ) -> None:
        self._conn.execute(
            "UPDATE deployments SET status = ?, finished_at = ?, error = ?, logs = ? "
            "WHERE id = ?",
            (STATUS_FAILED, _now(), error, logs, deployment_id),
        )
        self._conn.commit()

    def get_deployment(self, deployment_id: int) -> Optional[Dict[str, Any]]:
        return self._row_to_dict(
            self._conn.execute(
                "SELECT * FROM deployments WHERE id = ?", (deployment_id,)
            ).fetchone()
        )

    def list_deployments(self, app_name: str, limit: int = 20) -> List[Dict[str, Any]]:
        rows = self._conn.execute(
            "SELECT * FROM deployments WHERE app_name = ? ORDER BY id DESC LIMIT ?",
            (app_name, limit),
        ).fetchall()
        return [dict(r) for r in rows]

    def last_successful_deployment(self, app_name: str) -> Optional[Dict[str, Any]]:
        """Most recent successful deployment that is not the running one."""
        app = self.get_app(app_name)
        current = app["current_deployment"] if app else None
        row = self._conn.execute(
            "SELECT * FROM deployments WHERE app_name = ? AND status = ? "
            "AND (? IS NULL OR id != ?) ORDER BY id DESC LIMIT 1",
            (app_name, STATUS_SUCCESS, current, current),
        ).fetchone()
        return self._row_to_dict(row)

    def find_successful_deployment_by_sha(
        self, app_name: str, commit_sha: str
    ) -> Optional[Dict[str, Any]]:
        """Match on full SHA or on any prefix of it.

        The prefix is a LIKE pattern, so the caller's text has to be escaped:
        ``rollback --commit`` passes a raw command-line string, and an
        unescaped ``_`` or ``%`` is a wildcard that would match an arbitrary
        deployment and roll the application onto it.
        """
        row = self._conn.execute(
            "SELECT * FROM deployments WHERE app_name = ? AND status = ? "
            "AND commit_sha LIKE ? ESCAPE ? ORDER BY id DESC LIMIT 1",
            (
                app_name,
                STATUS_SUCCESS,
                f"{_escape_like(commit_sha)}%",
                _LIKE_ESCAPE_CHAR,
            ),
        ).fetchone()
        return self._row_to_dict(row)

    # ------------------------------------------------------------------- apps

    def get_app(self, app_name: str) -> Optional[Dict[str, Any]]:
        return self._row_to_dict(
            self._conn.execute(
                "SELECT * FROM apps WHERE app_name = ?", (app_name,)
            ).fetchone()
        )

    def current_container(self, app_name: str) -> Optional[str]:
        app = self.get_app(app_name)
        return app["current_container"] if app else None

    def list_apps(self) -> List[Dict[str, Any]]:
        rows = self._conn.execute(
            "SELECT * FROM apps ORDER BY app_name"
        ).fetchall()
        return [dict(r) for r in rows]

    def register(self, app_name: str) -> None:
        """Make an application visible before it has ever been deployed.

        ``record_start`` only creates an ``apps`` row as a side effect of a
        deployment attempt, so an application registered via ``init`` but not
        yet pushed had no row anywhere -- invisible to anything that reads
        this table. This exists purely so registration itself is visible.
        """
        self._ensure_app_row(app_name)
        self._conn.commit()

    def forget_app(self, app_name: str) -> None:
        self._conn.execute("DELETE FROM apps WHERE app_name = ?", (app_name,))
        self._conn.commit()

    def list_recent_deployments(self, limit: int = 15) -> List[Dict[str, Any]]:
        """The most recent deployment attempts across every application.

        Used for a platform-wide activity feed; per-application history is
        ``list_deployments``.
        """
        rows = self._conn.execute(
            "SELECT * FROM deployments ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()
        return [dict(r) for r in rows]

    # ---------------------------------------------------- supervisor counters

    def set_health(self, app_name: str, health_state: str) -> None:
        self._ensure_app_row(app_name)
        self._conn.execute(
            "UPDATE apps SET health_state = ? WHERE app_name = ?",
            (health_state, app_name),
        )
        self._conn.commit()

    def restart_attempts(self, app_name: str) -> int:
        app = self.get_app(app_name)
        return int(app["restart_attempts"]) if app else 0

    def increment_restart_attempts(self, app_name: str) -> int:
        self._ensure_app_row(app_name)
        self._conn.execute(
            "UPDATE apps SET restart_attempts = restart_attempts + 1 "
            "WHERE app_name = ?",
            (app_name,),
        )
        self._conn.commit()
        return self.restart_attempts(app_name)

    def reset_restart_attempts(self, app_name: str) -> None:
        self._ensure_app_row(app_name)
        self._conn.execute(
            "UPDATE apps SET restart_attempts = 0 WHERE app_name = ?", (app_name,)
        )
        self._conn.commit()

    def mark_failed(self, app_name: str) -> None:
        self._ensure_app_row(app_name)
        self._conn.execute(
            "UPDATE apps SET marked_failed = 1, health_state = 'failed' "
            "WHERE app_name = ?",
            (app_name,),
        )
        self._conn.commit()

    def clear_failed(self, app_name: str) -> None:
        self._ensure_app_row(app_name)
        self._conn.execute(
            "UPDATE apps SET marked_failed = 0 WHERE app_name = ?", (app_name,)
        )
        self._conn.commit()

    def is_marked_failed(self, app_name: str) -> bool:
        app = self.get_app(app_name)
        return bool(app["marked_failed"]) if app else False

    # -------------------------------------------------------------- databases

    def get_database(self, app_name: str) -> Optional[Dict[str, Any]]:
        return self._row_to_dict(
            self._conn.execute(
                "SELECT * FROM databases WHERE app_name = ?", (app_name,)
            ).fetchone()
        )

    def record_database(
        self,
        app_name: str,
        engine: str,
        container_name: str,
        volume_name: str,
        db_name: str,
        username: str,
        password: str,
    ) -> None:
        self._conn.execute(
            "INSERT INTO databases (app_name, engine, container_name, volume_name, "
            "db_name, username, password, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(app_name) DO UPDATE SET engine = excluded.engine, "
            "container_name = excluded.container_name, "
            "volume_name = excluded.volume_name, db_name = excluded.db_name, "
            "username = excluded.username, password = excluded.password",
            (
                app_name, engine, container_name, volume_name,
                db_name, username, password, _now(),
            ),
        )
        self._conn.commit()

    def delete_database(self, app_name: str) -> None:
        self._conn.execute("DELETE FROM databases WHERE app_name = ?", (app_name,))
        self._conn.commit()
