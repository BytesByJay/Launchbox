# Launchbox Deployment Safety Core — Implementation Plan (Plan 1 of 2)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace Launchbox's destroy-then-recreate deployment with a health-gated swap backed by a persistent deployment history, so that a failed build or a failed health probe leaves the previously running application serving traffic, and any earlier successful version can be rolled back to without rebuilding.

**Architecture:** Routing moves off immutable Docker labels onto Traefik's file provider, which lets a new container be created and verified *before* it receives any traffic. A new `deploy.py` orchestrator owns step ordering and the health gate; `builder.py` and `runner.py` become libraries it calls. A SQLite store records every deployment attempt, the current container per app, and supervisor state. A background supervisor restarts unhealthy containers within a bounded retry count.

**Tech Stack:** Python 3.12, Docker CLI via `subprocess`, Traefik v2.10 file provider, SQLite via stdlib `sqlite3`, pytest + pytest-mock.

**Spec:** `docs/superpowers/specs/2026-08-07-launchbox-completion-design.md`

## Global Constraints

- All Python runs from the project virtualenv: `.venv/bin/python`, `.venv/bin/pytest`.
- `BASE_DIR`, `APPS_DIR`, `REPOS_DIR` come from `launchbox/config.py`. Never hardcode paths.
- The Docker network is `traefik_default`.
- Image naming: `launchbox-<app_name>:<short_sha>`, plus a moving `launchbox-<app_name>:latest`.
- Container naming: `<app_name>-<short_sha>`. Short SHA is the first 7 characters.
- Traefik dynamic route files live in `traefik/dynamic/<app_name>.yml`.
- The SQLite database lives at `state/launchbox.db`. `state/` is gitignored.
- Every unit test must pass with **no Docker daemon running**. Tests needing Docker are marked `@pytest.mark.docker`.
- Existing public functions `builder.build(app_name)` and `runner.run(app_name)` must keep working with a single positional argument, because `dashboard.py` calls them that way and is not modified until Plan 2.
- Exception types come from `launchbox/logger.py`: `LaunchboxError`, `ConfigurationError`, `BuildError`, `DeploymentError`.
- Commit after every task. Never use `git add -A`; list files explicitly.

## File Structure

**Created:**

| File | Responsibility |
|------|----------------|
| `launchbox/state.py` | SQLite metadata store. Deployment history, current-container pointers, supervisor counters, database credentials. |
| `launchbox/router.py` | Generates, writes and removes Traefik dynamic route files. Pure YAML generation plus atomic file writes. |
| `launchbox/deploy.py` | Deployment orchestrator. Ordering, health gate, promotion, rollback. |
| `launchbox/supervisor.py` | Health polling loop with bounded restarts. |
| `launchbox/__main__.py` | Unified CLI. |
| `pytest.ini` | pytest configuration and marker registration. |
| `tests/conftest.py` | Shared fixtures. |
| `tests/test_state.py`, `tests/test_router.py`, `tests/test_builder.py`, `tests/test_runner.py`, `tests/test_deploy.py`, `tests/test_rollback.py`, `tests/test_supervisor.py`, `tests/test_hook.py` | Unit tests. |

**Modified:**

| File | Change |
|------|--------|
| `launchbox/builder.py` | Accept a source directory and a tag; produce SHA-tagged images. |
| `launchbox/runner.py` | Split into container lifecycle primitives. Stop emitting Traefik labels. Add restart policy. |
| `launchbox/init.py` | Rewrite the generated hook: stdin parsing, branch filter, temp-worktree checkout, single orchestrator call. |
| `docker-compose.yml` | Dynamic-config mount path, Traefik dashboard port. |
| `requirements.txt` | Add pytest, pytest-mock. Drop the dead `pathlib` backport. |
| `.gitignore` | Ignore `state/` and `traefik/dynamic/*.yml`. |
| `setup.sh` | Create `state/` and `traefik/dynamic/`. |

---

### Task 1: Test harness and scaffolding

**Files:**
- Create: `pytest.ini`, `tests/__init__.py`, `tests/conftest.py`, `tests/test_scaffolding.py`, `traefik/dynamic/.gitkeep`
- Modify: `requirements.txt`, `.gitignore`, `setup.sh`

**Interfaces:**
- Consumes: nothing.
- Produces: pytest fixture `tmp_state_db(tmp_path) -> str` returning a path to a fresh SQLite file; marker `docker` registered so `@pytest.mark.docker` does not warn.

- [ ] **Step 1: Update requirements.txt**

Replace the file contents with:

```
# Launchbox Dependencies
pyyaml>=6.0
docker>=6.0.0
flask>=2.3.0

# Test dependencies
pytest>=8.0
pytest-mock>=3.12
```

`pathlib` is removed deliberately: it has been in the standard library since Python 3.4, and the PyPI package of that name is an abandoned backport that shadows it.

- [ ] **Step 2: Create pytest.ini**

```ini
[pytest]
testpaths = tests
python_files = test_*.py
markers =
    docker: test requires a running Docker daemon (deselect with '-m "not docker"')
filterwarnings =
    error::DeprecationWarning:launchbox.*
```

- [ ] **Step 3: Create tests/__init__.py**

Empty file.

```bash
touch tests/__init__.py
```

- [ ] **Step 4: Create tests/conftest.py**

```python
import shutil
import subprocess

import pytest


@pytest.fixture
def tmp_state_db(tmp_path):
    """Path to a fresh, non-existent SQLite file inside a temp directory."""
    return str(tmp_path / "launchbox-test.db")


@pytest.fixture
def tmp_dynamic_dir(tmp_path):
    """An empty directory standing in for traefik/dynamic/."""
    d = tmp_path / "dynamic"
    d.mkdir()
    return str(d)


def _docker_available():
    if shutil.which("docker") is None:
        return False
    try:
        result = subprocess.run(
            ["docker", "info"],
            capture_output=True,
            timeout=10,
            check=False,
        )
        return result.returncode == 0
    except Exception:
        return False


def pytest_runtest_setup(item):
    """Skip @pytest.mark.docker tests when no daemon is reachable."""
    if item.get_closest_marker("docker") and not _docker_available():
        pytest.skip("Docker daemon not available")
```

- [ ] **Step 5: Create tests/test_scaffolding.py**

```python
import pytest


def test_launchbox_package_imports():
    import launchbox.config

    assert launchbox.config.APPS_DIR.endswith("apps")
    assert launchbox.config.REPOS_DIR.endswith("repos")


def test_tmp_state_db_fixture_gives_unused_path(tmp_state_db):
    import os

    assert not os.path.exists(tmp_state_db)


@pytest.mark.docker
def test_docker_marker_is_registered():
    import subprocess

    result = subprocess.run(["docker", "info"], capture_output=True, check=False)
    assert result.returncode == 0
```

- [ ] **Step 6: Run the tests**

Run: `.venv/bin/pytest -v`
Expected: 3 passed (the third may show as passed since Docker is running here; on a machine without Docker it skips).

- [ ] **Step 7: Add the ignore rules**

Append to `.gitignore`:

```
# Launchbox runtime state
state/

# Generated Traefik dynamic routes
traefik/dynamic/*.yml
!traefik/dynamic/.gitkeep
```

- [ ] **Step 8: Create the dynamic route directory**

```bash
mkdir -p traefik/dynamic && touch traefik/dynamic/.gitkeep
```

- [ ] **Step 9: Update setup.sh**

In `setup.sh`, change the directory creation line from:

```bash
mkdir -p apps repos letsencrypt certs traefik logs
```

to:

```bash
mkdir -p apps repos letsencrypt certs traefik/dynamic logs state
```

and change the permissions line from:

```bash
chmod 755 apps repos certs traefik logs
```

to:

```bash
chmod 755 apps repos certs traefik traefik/dynamic logs
chmod 700 state
```

- [ ] **Step 10: Verify setup.sh still runs**

Run: `bash -n setup.sh`
Expected: no output (syntax valid).

- [ ] **Step 11: Commit**

```bash
git add pytest.ini tests/__init__.py tests/conftest.py tests/test_scaffolding.py \
        requirements.txt .gitignore setup.sh traefik/dynamic/.gitkeep
git commit -m "test: add pytest harness and runtime state scaffolding

Registers the docker marker so integration tests auto-skip without a
daemon, adds shared fixtures, and creates the state/ and traefik/dynamic/
directories the orchestrator will need.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```

---

### Task 2: SQLite state store

**Files:**
- Create: `launchbox/state.py`
- Test: `tests/test_state.py`

**Interfaces:**
- Consumes: `launchbox.config.BASE_DIR`.
- Produces: class `StateStore` with this exact surface, used by Tasks 6, 8 and 9:

```python
StateStore(db_path: str = DEFAULT_DB_PATH)
    .close() -> None
    # also a context manager: `with StateStore() as store:` (used by the CLI)
    # deployments
    .record_start(app_name: str, commit_sha: str | None) -> int
    .record_success(deployment_id: int, container_name: str, image_tag: str) -> None
    .record_failure(deployment_id: int, error: str, logs: str | None = None) -> None
    .get_deployment(deployment_id: int) -> dict | None
    .list_deployments(app_name: str, limit: int = 20) -> list[dict]
    .last_successful_deployment(app_name: str) -> dict | None
    .find_successful_deployment_by_sha(app_name: str, commit_sha: str) -> dict | None
    # apps
    .get_app(app_name: str) -> dict | None
    .current_container(app_name: str) -> str | None
    .list_apps() -> list[dict]
    .forget_app(app_name: str) -> None
    # supervisor counters
    .set_health(app_name: str, health_state: str) -> None
    .restart_attempts(app_name: str) -> int
    .increment_restart_attempts(app_name: str) -> int
    .reset_restart_attempts(app_name: str) -> None
    .mark_failed(app_name: str) -> None
    .clear_failed(app_name: str) -> None
    .is_marked_failed(app_name: str) -> bool
    # database credentials (consumed by Plan 2)
    .get_database(app_name: str) -> dict | None
    .record_database(app_name: str, engine: str, container_name: str,
                     volume_name: str, db_name: str, username: str,
                     password: str) -> None
    .delete_database(app_name: str) -> None
```

Module constant `DEFAULT_DB_PATH = os.path.join(BASE_DIR, "state", "launchbox.db")`.
Status values are the literals `"in_progress"`, `"success"`, `"failed"`.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_state.py`:

```python
import pytest

from launchbox.state import StateStore


@pytest.fixture
def store(tmp_state_db):
    s = StateStore(tmp_state_db)
    yield s
    s.close()


def test_record_start_creates_in_progress_deployment(store):
    dep_id = store.record_start("myapp", "abc1234def")
    dep = store.get_deployment(dep_id)

    assert dep["app_name"] == "myapp"
    assert dep["commit_sha"] == "abc1234def"
    assert dep["status"] == "in_progress"
    assert dep["started_at"] is not None
    assert dep["finished_at"] is None


def test_record_success_updates_deployment_and_app_pointer(store):
    dep_id = store.record_start("myapp", "abc1234")
    store.record_success(dep_id, "myapp-abc1234", "launchbox-myapp:abc1234")

    dep = store.get_deployment(dep_id)
    assert dep["status"] == "success"
    assert dep["container_name"] == "myapp-abc1234"
    assert dep["image_tag"] == "launchbox-myapp:abc1234"
    assert dep["finished_at"] is not None

    assert store.current_container("myapp") == "myapp-abc1234"
    app = store.get_app("myapp")
    assert app["current_image"] == "launchbox-myapp:abc1234"
    assert app["current_deployment"] == dep_id


def test_record_failure_does_not_move_the_app_pointer(store):
    good = store.record_start("myapp", "aaaaaaa")
    store.record_success(good, "myapp-aaaaaaa", "launchbox-myapp:aaaaaaa")

    bad = store.record_start("myapp", "bbbbbbb")
    store.record_failure(bad, "health probe failed", logs="traceback here")

    assert store.get_deployment(bad)["status"] == "failed"
    assert store.get_deployment(bad)["error"] == "health probe failed"
    assert store.get_deployment(bad)["logs"] == "traceback here"
    # The critical property: a failure never repoints the app.
    assert store.current_container("myapp") == "myapp-aaaaaaa"


def test_current_container_is_none_for_unknown_app(store):
    assert store.current_container("nope") is None
    assert store.get_app("nope") is None


def test_list_deployments_returns_newest_first(store):
    first = store.record_start("myapp", "1111111")
    store.record_success(first, "myapp-1111111", "launchbox-myapp:1111111")
    second = store.record_start("myapp", "2222222")
    store.record_success(second, "myapp-2222222", "launchbox-myapp:2222222")

    deps = store.list_deployments("myapp")
    assert [d["id"] for d in deps] == [second, first]


def test_list_deployments_respects_limit_and_app_filter(store):
    for i in range(5):
        dep = store.record_start("myapp", f"sha{i}")
        store.record_success(dep, f"myapp-sha{i}", f"launchbox-myapp:sha{i}")
    other = store.record_start("otherapp", "zzz")
    store.record_success(other, "otherapp-zzz", "launchbox-otherapp:zzz")

    deps = store.list_deployments("myapp", limit=2)
    assert len(deps) == 2
    assert all(d["app_name"] == "myapp" for d in deps)


def test_last_successful_deployment_excludes_the_current_one(store):
    first = store.record_start("myapp", "1111111")
    store.record_success(first, "myapp-1111111", "launchbox-myapp:1111111")
    second = store.record_start("myapp", "2222222")
    store.record_success(second, "myapp-2222222", "launchbox-myapp:2222222")

    target = store.last_successful_deployment("myapp")
    assert target["id"] == first, "rollback target must not be the running version"


def test_last_successful_deployment_skips_failures(store):
    good = store.record_start("myapp", "1111111")
    store.record_success(good, "myapp-1111111", "launchbox-myapp:1111111")
    running = store.record_start("myapp", "2222222")
    store.record_success(running, "myapp-2222222", "launchbox-myapp:2222222")
    broken = store.record_start("myapp", "3333333")
    store.record_failure(broken, "build failed")

    target = store.last_successful_deployment("myapp")
    assert target["id"] == good


def test_last_successful_deployment_is_none_with_only_one_success(store):
    only = store.record_start("myapp", "1111111")
    store.record_success(only, "myapp-1111111", "launchbox-myapp:1111111")

    assert store.last_successful_deployment("myapp") is None


def test_find_successful_deployment_by_sha(store):
    dep = store.record_start("myapp", "abc1234")
    store.record_success(dep, "myapp-abc1234", "launchbox-myapp:abc1234")
    failed = store.record_start("myapp", "bad0000")
    store.record_failure(failed, "nope")

    assert store.find_successful_deployment_by_sha("myapp", "abc1234")["id"] == dep
    assert store.find_successful_deployment_by_sha("myapp", "bad0000") is None
    assert store.find_successful_deployment_by_sha("myapp", "nothere") is None


def test_find_successful_deployment_by_short_sha(store):
    dep = store.record_start("myapp", "abc1234deadbeef")
    store.record_success(dep, "myapp-abc1234", "launchbox-myapp:abc1234")

    assert store.find_successful_deployment_by_sha("myapp", "abc1234")["id"] == dep


def test_restart_attempts_increment_and_reset(store):
    dep = store.record_start("myapp", "abc1234")
    store.record_success(dep, "myapp-abc1234", "launchbox-myapp:abc1234")

    assert store.restart_attempts("myapp") == 0
    assert store.increment_restart_attempts("myapp") == 1
    assert store.increment_restart_attempts("myapp") == 2
    assert store.restart_attempts("myapp") == 2

    store.reset_restart_attempts("myapp")
    assert store.restart_attempts("myapp") == 0


def test_mark_failed_and_clear(store):
    dep = store.record_start("myapp", "abc1234")
    store.record_success(dep, "myapp-abc1234", "launchbox-myapp:abc1234")

    assert store.is_marked_failed("myapp") is False
    store.mark_failed("myapp")
    assert store.is_marked_failed("myapp") is True
    store.clear_failed("myapp")
    assert store.is_marked_failed("myapp") is False


def test_set_health_records_state(store):
    dep = store.record_start("myapp", "abc1234")
    store.record_success(dep, "myapp-abc1234", "launchbox-myapp:abc1234")

    store.set_health("myapp", "unhealthy")
    assert store.get_app("myapp")["health_state"] == "unhealthy"


def test_supervisor_counters_work_for_app_never_deployed(store):
    """Supervisor must not crash on an app with no deployment row."""
    assert store.restart_attempts("ghost") == 0
    store.increment_restart_attempts("ghost")
    assert store.restart_attempts("ghost") == 1


def test_list_apps_returns_all_known_apps(store):
    for name in ("alpha", "beta"):
        dep = store.record_start(name, "1111111")
        store.record_success(dep, f"{name}-1111111", f"launchbox-{name}:1111111")

    names = sorted(a["app_name"] for a in store.list_apps())
    assert names == ["alpha", "beta"]


def test_forget_app_removes_pointer_but_keeps_history(store):
    dep = store.record_start("myapp", "abc1234")
    store.record_success(dep, "myapp-abc1234", "launchbox-myapp:abc1234")

    store.forget_app("myapp")
    assert store.get_app("myapp") is None
    assert len(store.list_deployments("myapp")) == 1


def test_database_credentials_roundtrip(store):
    assert store.get_database("myapp") is None

    store.record_database(
        "myapp", "postgresql", "myapp_postgres", "myapp_postgresql_data",
        "myapp_db", "lb_myapp", "s3cret",
    )
    db = store.get_database("myapp")

    assert db["engine"] == "postgresql"
    assert db["container_name"] == "myapp_postgres"
    assert db["volume_name"] == "myapp_postgresql_data"
    assert db["db_name"] == "myapp_db"
    assert db["username"] == "lb_myapp"
    assert db["password"] == "s3cret"


def test_record_database_is_idempotent_on_reregistration(store):
    store.record_database("myapp", "postgresql", "myapp_postgres",
                          "vol", "myapp_db", "lb_myapp", "first")
    store.record_database("myapp", "postgresql", "myapp_postgres",
                          "vol", "myapp_db", "lb_myapp", "second")

    assert store.get_database("myapp")["password"] == "second"
    assert len([d for d in store.list_apps()]) >= 0  # no duplicate-key crash


def test_delete_database(store):
    store.record_database("myapp", "mysql", "myapp_mysql", "vol",
                          "myapp_db", "lb_myapp", "pw")
    store.delete_database("myapp")
    assert store.get_database("myapp") is None


def test_store_creates_parent_directory(tmp_path):
    nested = tmp_path / "deep" / "nested" / "state.db"
    s = StateStore(str(nested))
    try:
        assert nested.exists()
    finally:
        s.close()


def test_reopening_store_preserves_data(tmp_state_db):
    s1 = StateStore(tmp_state_db)
    dep = s1.record_start("myapp", "abc1234")
    s1.record_success(dep, "myapp-abc1234", "launchbox-myapp:abc1234")
    s1.close()

    s2 = StateStore(tmp_state_db)
    try:
        assert s2.current_container("myapp") == "myapp-abc1234"
    finally:
        s2.close()
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `.venv/bin/pytest tests/test_state.py -v`
Expected: collection error, `ModuleNotFoundError: No module named 'launchbox.state'`.

- [ ] **Step 3: Write the implementation**

Create `launchbox/state.py`:

```python
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
    logs           TEXT
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

    def record_start(self, app_name: str, commit_sha: Optional[str]) -> int:
        cur = self._conn.execute(
            "INSERT INTO deployments (app_name, commit_sha, status, started_at) "
            "VALUES (?, ?, ?, ?)",
            (app_name, commit_sha, STATUS_IN_PROGRESS, _now()),
        )
        self._ensure_app_row(app_name)
        self._conn.commit()
        return int(cur.lastrowid)

    def record_success(
        self, deployment_id: int, container_name: str, image_tag: str
    ) -> None:
        row = self._conn.execute(
            "SELECT app_name FROM deployments WHERE id = ?", (deployment_id,)
        ).fetchone()
        if row is None:
            raise KeyError(f"Unknown deployment id: {deployment_id}")
        app_name = row["app_name"]

        self._conn.execute(
            "UPDATE deployments SET status = ?, container_name = ?, image_tag = ?, "
            "finished_at = ? WHERE id = ?",
            (STATUS_SUCCESS, container_name, image_tag, _now(), deployment_id),
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
        """Match on full SHA or on any prefix of it."""
        row = self._conn.execute(
            "SELECT * FROM deployments WHERE app_name = ? AND status = ? "
            "AND commit_sha LIKE ? ORDER BY id DESC LIMIT 1",
            (app_name, STATUS_SUCCESS, f"{commit_sha}%"),
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

    def forget_app(self, app_name: str) -> None:
        self._conn.execute("DELETE FROM apps WHERE app_name = ?", (app_name,))
        self._conn.commit()

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
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `.venv/bin/pytest tests/test_state.py -v`
Expected: all tests PASS.

- [ ] **Step 5: Commit**

```bash
git add launchbox/state.py tests/test_state.py
git commit -m "feat: add SQLite deployment state store

Records deployment history, the current container per application,
supervisor restart counters and per-application database credentials.
A failed deployment never moves the current-container pointer, which is
what makes rollback and the health gate possible.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```

---

### Task 3: Traefik file-provider router

**Files:**
- Create: `launchbox/router.py`
- Test: `tests/test_router.py`
- Modify: `docker-compose.yml`

**Interfaces:**
- Consumes: `launchbox.config.BASE_DIR`.
- Produces:

```python
DYNAMIC_DIR: str = os.path.join(BASE_DIR, "traefik", "dynamic")

route_file_path(app_name: str, dynamic_dir: str = DYNAMIC_DIR) -> str
build_route_document(app_name: str, backend_host: str, port: int,
                     https: bool = False, redirect_http: bool = True) -> dict
write_route(app_name: str, backend_host: str, port: int,
            https: bool = False, redirect_http: bool = True,
            dynamic_dir: str = DYNAMIC_DIR) -> str
remove_route(app_name: str, dynamic_dir: str = DYNAMIC_DIR) -> bool
read_route(app_name: str, dynamic_dir: str = DYNAMIC_DIR) -> dict | None
```

`backend_host` is the container name; Docker's embedded DNS on `traefik_default` resolves it.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_router.py`:

```python
import os

import yaml

from launchbox import router


def test_route_document_http_only():
    doc = router.build_route_document("myapp", "myapp-abc1234", 3000)

    assert doc["http"]["routers"]["myapp"]["rule"] == "Host(`myapp.localhost`)"
    assert doc["http"]["routers"]["myapp"]["service"] == "myapp-svc"
    assert doc["http"]["routers"]["myapp"]["entryPoints"] == ["web"]
    assert "tls" not in doc["http"]["routers"]["myapp"]

    servers = doc["http"]["services"]["myapp-svc"]["loadBalancer"]["servers"]
    assert servers == [{"url": "http://myapp-abc1234:3000"}]


def test_route_document_https_adds_secure_router():
    doc = router.build_route_document(
        "myapp", "myapp-abc1234", 8080, https=True, redirect_http=False
    )
    secure = doc["http"]["routers"]["myapp-secure"]

    assert secure["rule"] == "Host(`myapp.localhost`)"
    assert secure["entryPoints"] == ["websecure"]
    assert secure["tls"] == {}
    assert secure["service"] == "myapp-svc"
    # Both routers share one backend service definition.
    assert len(doc["http"]["services"]) == 1


def test_route_document_https_with_redirect_adds_middleware():
    doc = router.build_route_document(
        "myapp", "myapp-abc1234", 3000, https=True, redirect_http=True
    )

    assert doc["http"]["routers"]["myapp"]["middlewares"] == ["myapp-redirect"]
    middleware = doc["http"]["middlewares"]["myapp-redirect"]
    assert middleware["redirectScheme"]["scheme"] == "https"


def test_route_document_without_https_has_no_middlewares_key():
    doc = router.build_route_document("myapp", "myapp-abc1234", 3000)
    assert "middlewares" not in doc["http"]


def test_write_route_creates_parseable_yaml(tmp_dynamic_dir):
    path = router.write_route(
        "myapp", "myapp-abc1234", 3000, dynamic_dir=tmp_dynamic_dir
    )

    assert path == os.path.join(tmp_dynamic_dir, "myapp.yml")
    assert os.path.exists(path)

    with open(path) as handle:
        parsed = yaml.safe_load(handle)
    assert parsed["http"]["services"]["myapp-svc"]["loadBalancer"]["servers"] == [
        {"url": "http://myapp-abc1234:3000"}
    ]


def test_write_route_overwrites_previous_backend(tmp_dynamic_dir):
    router.write_route("myapp", "myapp-1111111", 3000, dynamic_dir=tmp_dynamic_dir)
    router.write_route("myapp", "myapp-2222222", 3000, dynamic_dir=tmp_dynamic_dir)

    doc = router.read_route("myapp", dynamic_dir=tmp_dynamic_dir)
    servers = doc["http"]["services"]["myapp-svc"]["loadBalancer"]["servers"]
    assert servers == [{"url": "http://myapp-2222222:3000"}]


def test_write_route_leaves_no_temp_files_behind(tmp_dynamic_dir):
    router.write_route("myapp", "myapp-abc1234", 3000, dynamic_dir=tmp_dynamic_dir)

    assert sorted(os.listdir(tmp_dynamic_dir)) == ["myapp.yml"]


def test_write_route_creates_missing_directory(tmp_path):
    target = str(tmp_path / "not-yet-there")
    router.write_route("myapp", "myapp-abc1234", 3000, dynamic_dir=target)

    assert os.path.exists(os.path.join(target, "myapp.yml"))


def test_remove_route_deletes_the_file(tmp_dynamic_dir):
    router.write_route("myapp", "myapp-abc1234", 3000, dynamic_dir=tmp_dynamic_dir)

    assert router.remove_route("myapp", dynamic_dir=tmp_dynamic_dir) is True
    assert not os.path.exists(os.path.join(tmp_dynamic_dir, "myapp.yml"))


def test_remove_route_is_safe_when_absent(tmp_dynamic_dir):
    assert router.remove_route("ghost", dynamic_dir=tmp_dynamic_dir) is False


def test_read_route_returns_none_when_absent(tmp_dynamic_dir):
    assert router.read_route("ghost", dynamic_dir=tmp_dynamic_dir) is None
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `.venv/bin/pytest tests/test_router.py -v`
Expected: `ModuleNotFoundError: No module named 'launchbox.router'`.

- [ ] **Step 3: Write the implementation**

Create `launchbox/router.py`:

```python
"""Traefik dynamic-route generation.

Routes are registered by writing a small YAML document into the directory
Traefik's file provider watches, rather than by attaching labels to the
application container.

The distinction matters: Docker labels are fixed at container creation, so a
label-routed container is reachable from the instant it exists. Writing the
route as a separate file lets the orchestrator create a container, verify it is
healthy, and only then send traffic to it -- and lets a failed deployment be
discarded without the route ever having moved.
"""

import os
from typing import Any, Dict, Optional

import yaml

from launchbox.config import BASE_DIR
from launchbox.logger import setup_logger

logger = setup_logger("router")

DYNAMIC_DIR = os.path.join(BASE_DIR, "traefik", "dynamic")


def route_file_path(app_name: str, dynamic_dir: str = DYNAMIC_DIR) -> str:
    return os.path.join(dynamic_dir, f"{app_name}.yml")


def build_route_document(
    app_name: str,
    backend_host: str,
    port: int,
    https: bool = False,
    redirect_http: bool = True,
) -> Dict[str, Any]:
    """Build the Traefik dynamic configuration for one application."""
    service_name = f"{app_name}-svc"
    host_rule = f"Host(`{app_name}.localhost`)"

    web_router: Dict[str, Any] = {
        "rule": host_rule,
        "service": service_name,
        "entryPoints": ["web"],
    }

    http_section: Dict[str, Any] = {
        "routers": {app_name: web_router},
        "services": {
            service_name: {
                "loadBalancer": {
                    "servers": [{"url": f"http://{backend_host}:{port}"}]
                }
            }
        },
    }

    if https:
        http_section["routers"][f"{app_name}-secure"] = {
            "rule": host_rule,
            "service": service_name,
            "entryPoints": ["websecure"],
            "tls": {},
        }
        if redirect_http:
            middleware_name = f"{app_name}-redirect"
            web_router["middlewares"] = [middleware_name]
            http_section["middlewares"] = {
                middleware_name: {"redirectScheme": {"scheme": "https"}}
            }

    return {"http": http_section}


def write_route(
    app_name: str,
    backend_host: str,
    port: int,
    https: bool = False,
    redirect_http: bool = True,
    dynamic_dir: str = DYNAMIC_DIR,
) -> str:
    """Write the route file atomically and return its path.

    The document is written to a temporary file in the same directory and then
    renamed over the target, so Traefik's watcher never observes a partial
    document.
    """
    os.makedirs(dynamic_dir, exist_ok=True)
    document = build_route_document(
        app_name, backend_host, port, https=https, redirect_http=redirect_http
    )

    target = route_file_path(app_name, dynamic_dir)
    tmp_target = f"{target}.tmp"

    with open(tmp_target, "w") as handle:
        yaml.safe_dump(document, handle, default_flow_style=False, sort_keys=False)
    os.replace(tmp_target, target)

    logger.info(
        f"Route registered: {app_name}.localhost -> {backend_host}:{port}"
        f"{' (https)' if https else ''}"
    )
    return target


def read_route(
    app_name: str, dynamic_dir: str = DYNAMIC_DIR
) -> Optional[Dict[str, Any]]:
    path = route_file_path(app_name, dynamic_dir)
    if not os.path.exists(path):
        return None
    with open(path) as handle:
        return yaml.safe_load(handle)


def remove_route(app_name: str, dynamic_dir: str = DYNAMIC_DIR) -> bool:
    """Delete an application's route file. Returns True if one was removed."""
    path = route_file_path(app_name, dynamic_dir)
    if not os.path.exists(path):
        return False
    os.remove(path)
    logger.info(f"Route removed: {app_name}")
    return True
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `.venv/bin/pytest tests/test_router.py -v`
Expected: all tests PASS.

- [ ] **Step 5: Fix the Traefik mount and dashboard port**

In `docker-compose.yml`, change the volume line from:

```yaml
      - "./traefik:/etc/traefik/dynamic:ro"
```

to:

```yaml
      - "./traefik/dynamic:/etc/traefik/dynamic:ro"
```

`traefik/traefik.yml` is a *static* configuration file that currently sits inside the watched directory. The file provider would try to parse it as dynamic configuration and log errors on every reload.

Then change the ports block from:

```yaml
      - "8080:8080"
```

to:

```yaml
      - "8090:8080"
```

Port 8080 is occupied on the development host by an unrelated process.

- [ ] **Step 6: Verify the compose file still parses**

Run: `docker compose config -q`
Expected: no output (valid).

- [ ] **Step 7: Commit**

```bash
git add launchbox/router.py tests/test_router.py docker-compose.yml
git commit -m "feat: route through Traefik's file provider

Routes are written as watched YAML documents rather than attached as
container labels, so a container can be created and verified before it
receives traffic. Moves the dynamic mount off the directory holding the
static config, and frees port 8080.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```

---

### Task 4: Commit-versioned image builds

**Files:**
- Modify: `launchbox/builder.py`
- Test: `tests/test_builder.py`

**Interfaces:**
- Consumes: `LaunchboxConfig`, `BuildError`.
- Produces:

```python
build(app_name: str, source_dir: str | None = None,
      tag: str | None = None) -> str
```

Returns the full image reference, e.g. `launchbox-myapp:abc1234`. When `tag` is
omitted the reference is `launchbox-myapp:latest`. When `tag` is given, the
image is tagged *both* `:<tag>` and `:latest`, so old images survive for
rollback while `:latest` keeps pointing at the newest build.

`source_dir` defaults to `APPS_DIR/<app_name>`, preserving the existing
single-argument call from `dashboard.py`.

**Breaking change note:** `build()` previously returned `True`. It now returns a
string, which is truthy, so existing `if build_success:` checks still behave
correctly.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_builder.py`:

```python
import os
import subprocess

import pytest

from launchbox import builder
from launchbox.logger import BuildError


@pytest.fixture
def app_dir(tmp_path):
    """A minimal application directory with a Dockerfile and config."""
    d = tmp_path / "myapp"
    d.mkdir()
    (d / "Dockerfile").write_text("FROM scratch\n")
    (d / "launchbox.yaml").write_text("app:\n  port: 3000\n")
    return str(d)


def _ok(*_args, **_kwargs):
    return subprocess.CompletedProcess(args=[], returncode=0, stdout="", stderr="")


def test_build_returns_tagged_image_reference(mocker, app_dir):
    mocker.patch("launchbox.builder.subprocess.run", side_effect=_ok)

    ref = builder.build("myapp", source_dir=app_dir, tag="abc1234")

    assert ref == "launchbox-myapp:abc1234"


def test_build_without_tag_uses_latest(mocker, app_dir):
    mocker.patch("launchbox.builder.subprocess.run", side_effect=_ok)

    assert builder.build("myapp", source_dir=app_dir) == "launchbox-myapp:latest"


def test_build_tags_both_sha_and_latest(mocker, app_dir):
    run = mocker.patch("launchbox.builder.subprocess.run", side_effect=_ok)

    builder.build("myapp", source_dir=app_dir, tag="abc1234")

    build_cmd = run.call_args_list[0][0][0]
    assert "-t" in build_cmd
    tags = [build_cmd[i + 1] for i, a in enumerate(build_cmd) if a == "-t"]
    assert "launchbox-myapp:abc1234" in tags
    assert "launchbox-myapp:latest" in tags


def test_build_uses_the_given_source_directory(mocker, app_dir):
    run = mocker.patch("launchbox.builder.subprocess.run", side_effect=_ok)

    builder.build("myapp", source_dir=app_dir, tag="abc1234")

    cmd = run.call_args_list[0][0][0]
    assert cmd[-1] == app_dir
    assert os.path.join(app_dir, "Dockerfile") in cmd


def test_build_raises_when_source_directory_missing(tmp_path):
    with pytest.raises(BuildError, match="not found"):
        builder.build("myapp", source_dir=str(tmp_path / "nope"))


def test_build_raises_when_dockerfile_missing(tmp_path):
    d = tmp_path / "myapp"
    d.mkdir()
    with pytest.raises(BuildError, match="Dockerfile not found"):
        builder.build("myapp", source_dir=str(d))


def test_build_raises_with_docker_stderr_on_failure(mocker, app_dir):
    mocker.patch(
        "launchbox.builder.subprocess.run",
        return_value=subprocess.CompletedProcess(
            args=[], returncode=1, stdout="", stderr="step 3 failed"
        ),
    )

    with pytest.raises(BuildError, match="step 3 failed"):
        builder.build("myapp", source_dir=app_dir, tag="abc1234")


def test_build_honours_custom_dockerfile_and_context(mocker, tmp_path):
    d = tmp_path / "myapp"
    (d / "docker").mkdir(parents=True)
    (d / "docker" / "Dockerfile.prod").write_text("FROM scratch\n")
    (d / "src").mkdir()
    (d / "launchbox.yaml").write_text(
        "app:\n"
        "  build:\n"
        "    dockerfile: docker/Dockerfile.prod\n"
        "    context: src\n"
    )
    run = mocker.patch("launchbox.builder.subprocess.run", side_effect=_ok)

    builder.build("myapp", source_dir=str(d), tag="abc1234")

    cmd = run.call_args_list[0][0][0]
    assert str(d / "docker" / "Dockerfile.prod") in cmd
    assert cmd[-1] == str(d / "src")
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `.venv/bin/pytest tests/test_builder.py -v`
Expected: FAIL — `build()` does not accept `source_dir`, raising `TypeError: build() got an unexpected keyword argument 'source_dir'`.

- [ ] **Step 3: Rewrite builder.py**

Replace the whole of `launchbox/builder.py`:

```python
"""Docker image construction.

Images are tagged by commit so that a previous build survives a later one and
can be redeployed without rebuilding. A moving ``:latest`` tag always points at
the most recent successful build.
"""

import os
import subprocess
import sys
from typing import Optional

from launchbox.config import APPS_DIR
from launchbox.config_parser import LaunchboxConfig
from launchbox.logger import setup_logger, BuildError

logger = setup_logger("builder")


def image_name(app_name: str) -> str:
    return f"launchbox-{app_name}"


def build(
    app_name: str,
    source_dir: Optional[str] = None,
    tag: Optional[str] = None,
) -> str:
    """Build the application image and return its full reference.

    Args:
        app_name: application whose image is being built.
        source_dir: directory to build from. Defaults to ``apps/<app_name>``.
            The deployment pipeline passes a temporary worktree containing the
            exact commit that was pushed.
        tag: image tag, normally a short commit SHA. Defaults to ``latest``.

    Returns:
        The primary image reference, ``launchbox-<app>:<tag>``.
    """
    build_source = source_dir or os.path.join(APPS_DIR, app_name)

    if not os.path.isdir(build_source):
        raise BuildError(f"Application source directory not found: {build_source}")

    config = LaunchboxConfig(build_source)
    dockerfile = config.get_dockerfile()
    build_context = config.get_build_context()

    dockerfile_path = os.path.join(build_source, dockerfile)
    if not os.path.exists(dockerfile_path):
        raise BuildError(f"Dockerfile not found: {dockerfile_path}")

    base = image_name(app_name)
    effective_tag = tag or "latest"
    primary_ref = f"{base}:{effective_tag}"
    build_path = os.path.join(build_source, build_context)

    build_cmd = ["docker", "build", "-t", primary_ref]
    if effective_tag != "latest":
        build_cmd.extend(["-t", f"{base}:latest"])
    build_cmd.extend(["-f", dockerfile_path, build_path])

    logger.info(f"Building image: {primary_ref}")
    logger.info(f"Build context: {build_path}")
    logger.debug(f"Running command: {' '.join(build_cmd)}")

    result = subprocess.run(build_cmd, capture_output=True, text=True, check=False)

    if result.returncode != 0:
        logger.error(f"Docker build failed for {app_name}")
        logger.error(f"stdout: {result.stdout}")
        logger.error(f"stderr: {result.stderr}")
        raise BuildError(f"Docker build failed: {result.stderr}")

    logger.info(f"Successfully built image: {primary_ref}")
    return primary_ref


if __name__ == "__main__":
    if len(sys.argv) < 2:
        logger.error("Usage: python3 -m launchbox.builder <app_name>")
        sys.exit(1)

    try:
        build(sys.argv[1])
        sys.exit(0)
    except BuildError as exc:
        logger.error(f"Build failed: {exc}")
        sys.exit(1)
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `.venv/bin/pytest tests/test_builder.py -v`
Expected: all tests PASS.

- [ ] **Step 5: Commit**

```bash
git add launchbox/builder.py tests/test_builder.py
git commit -m "feat: tag images by commit and build from an explicit source

Images are now tagged launchbox-<app>:<short-sha> alongside a moving
:latest, so an earlier build survives a later one and can be redeployed
without rebuilding. build() accepts the temporary worktree the git hook
checks the pushed commit into.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```

---

### Task 5: Container lifecycle primitives

**Files:**
- Modify: `launchbox/runner.py`
- Test: `tests/test_runner.py`

**Interfaces:**
- Consumes: `LaunchboxConfig`, `DeploymentError`.
- Produces:

```python
NETWORK: str = "traefik_default"

parse_duration(value: str | int | float | None, default: float = 0.0) -> float
compute_health_timeout(health_check: dict | None) -> float
ensure_network(network: str = NETWORK) -> None
container_exists(container_name: str) -> bool
container_is_running(container_name: str) -> bool
image_exists(image_ref: str) -> bool
docker_health_status(container_name: str) -> str
    # one of: healthy, unhealthy, starting, none, missing
create_container(container_name: str, image_ref: str, port: int,
                 env_vars: dict, resource_limits: dict,
                 health_check: dict | None, app_name: str,
                 commit_sha: str | None = None,
                 network: str = NETWORK) -> str      # container id
wait_for_health(container_name: str, health_check: dict | None,
                timeout: float | None = None, settle_seconds: float = 5.0,
                poll_interval: float = 1.0) -> bool
container_logs(container_name: str, tail: int = 200) -> str
stop_and_remove(container_name: str) -> bool
restart_container(container_name: str) -> bool   # used by the supervisor, Task 9
build_health_command(port: int, path: str) -> str
run(app_name: str) -> bool      # backwards-compatible wrapper (Task 6)
```

Containers carry `launchbox.app`, `launchbox.service=app` and `launchbox.commit`
labels for discovery, and **no Traefik labels** — routing is the router's job.
Containers are created with `--restart unless-stopped`.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_runner.py`:

```python
import subprocess

import pytest

from launchbox import runner


def _completed(returncode=0, stdout="", stderr=""):
    return subprocess.CompletedProcess(
        args=[], returncode=returncode, stdout=stdout, stderr=stderr
    )


# --------------------------------------------------------------- parse_duration


@pytest.mark.parametrize(
    "value,expected",
    [
        ("30s", 30.0),
        ("10S", 10.0),
        ("2m", 120.0),
        ("1h", 3600.0),
        ("500ms", 0.5),
        ("45", 45.0),
        (15, 15.0),
        (2.5, 2.5),
    ],
)
def test_parse_duration_understands_traefik_style_strings(value, expected):
    assert runner.parse_duration(value) == expected


def test_parse_duration_falls_back_on_garbage():
    assert runner.parse_duration("banana", default=7.0) == 7.0
    assert runner.parse_duration(None, default=3.0) == 3.0


# --------------------------------------------------------- compute_health_timeout


def test_compute_health_timeout_covers_all_retries():
    timeout = runner.compute_health_timeout(
        {"interval": "5s", "timeout": "2s", "retries": 3}
    )
    # 5s x 3 retries + 2s probe timeout + grace, and never less than that sum
    assert timeout >= 17.0


def test_compute_health_timeout_has_a_default_for_no_healthcheck():
    assert runner.compute_health_timeout(None) > 0


def test_compute_health_timeout_honours_env_override(monkeypatch):
    monkeypatch.setenv("LAUNCHBOX_HEALTH_TIMEOUT", "12")
    assert runner.compute_health_timeout(
        {"interval": "30s", "timeout": "10s", "retries": 5}
    ) == 12.0


# ------------------------------------------------------------ docker_health_status


def test_docker_health_status_reports_healthy(mocker):
    mocker.patch(
        "launchbox.runner.subprocess.run",
        return_value=_completed(stdout="healthy\n"),
    )
    assert runner.docker_health_status("myapp-abc1234") == "healthy"


def test_docker_health_status_reports_none_for_container_without_probe(mocker):
    mocker.patch(
        "launchbox.runner.subprocess.run", return_value=_completed(stdout="none\n")
    )
    assert runner.docker_health_status("myapp-abc1234") == "none"


def test_docker_health_status_reports_missing_when_inspect_fails(mocker):
    mocker.patch(
        "launchbox.runner.subprocess.run",
        return_value=_completed(returncode=1, stderr="No such object"),
    )
    assert runner.docker_health_status("ghost") == "missing"


# --------------------------------------------------------------- create_container


def test_create_container_attaches_no_traefik_labels(mocker):
    run = mocker.patch(
        "launchbox.runner.subprocess.run", return_value=_completed(stdout="deadbeef\n")
    )
    mocker.patch("launchbox.runner.ensure_network")

    runner.create_container(
        container_name="myapp-abc1234",
        image_ref="launchbox-myapp:abc1234",
        port=3000,
        env_vars={},
        resource_limits={},
        health_check=None,
        app_name="myapp",
    )

    cmd = run.call_args[0][0]
    labels = [cmd[i + 1] for i, arg in enumerate(cmd) if arg == "--label"]
    assert not any("traefik" in label for label in labels), (
        "routing must come from the file provider, not container labels"
    )
    # The network is legitimately called traefik_default; only labels matter here.
    assert "--network" in cmd


def test_create_container_sets_restart_policy(mocker):
    run = mocker.patch(
        "launchbox.runner.subprocess.run", return_value=_completed(stdout="deadbeef\n")
    )
    mocker.patch("launchbox.runner.ensure_network")

    runner.create_container(
        container_name="myapp-abc1234",
        image_ref="launchbox-myapp:abc1234",
        port=3000,
        env_vars={},
        resource_limits={},
        health_check=None,
        app_name="myapp",
    )

    cmd = run.call_args[0][0]
    assert "--restart" in cmd
    assert cmd[cmd.index("--restart") + 1] == "unless-stopped"


def test_create_container_passes_env_limits_and_labels(mocker):
    run = mocker.patch(
        "launchbox.runner.subprocess.run", return_value=_completed(stdout="deadbeef\n")
    )
    mocker.patch("launchbox.runner.ensure_network")

    runner.create_container(
        container_name="myapp-abc1234",
        image_ref="launchbox-myapp:abc1234",
        port=3000,
        env_vars={"FOO": "bar"},
        resource_limits={"memory": "256m", "cpus": "0.5"},
        health_check=None,
        app_name="myapp",
        commit_sha="abc1234deadbeef",
    )

    cmd = run.call_args[0][0]
    assert "FOO=bar" in cmd
    assert "--memory" in cmd and "256m" in cmd
    assert "--cpus" in cmd and "0.5" in cmd
    assert "launchbox.app=myapp" in cmd
    assert "launchbox.service=app" in cmd
    assert "launchbox.commit=abc1234deadbeef" in cmd
    assert cmd[-1] == "launchbox-myapp:abc1234"


def test_create_container_adds_health_flags_when_configured(mocker):
    run = mocker.patch(
        "launchbox.runner.subprocess.run", return_value=_completed(stdout="deadbeef\n")
    )
    mocker.patch("launchbox.runner.ensure_network")

    runner.create_container(
        container_name="myapp-abc1234",
        image_ref="launchbox-myapp:abc1234",
        port=3000,
        env_vars={},
        resource_limits={},
        health_check={
            "path": "/health",
            "interval": "5s",
            "timeout": "2s",
            "retries": 2,
        },
        app_name="myapp",
    )

    cmd = run.call_args[0][0]
    assert "--health-cmd" in cmd
    assert "--health-interval" in cmd and "5s" in cmd
    assert "--health-timeout" in cmd and "2s" in cmd
    assert "--health-retries" in cmd and "2" in cmd
    health_cmd = cmd[cmd.index("--health-cmd") + 1]
    assert "http://localhost:3000/health" in health_cmd


def test_create_container_raises_on_docker_failure(mocker):
    mocker.patch(
        "launchbox.runner.subprocess.run",
        return_value=_completed(returncode=125, stderr="name already in use"),
    )
    mocker.patch("launchbox.runner.ensure_network")

    from launchbox.logger import DeploymentError

    with pytest.raises(DeploymentError, match="name already in use"):
        runner.create_container(
            container_name="myapp-abc1234",
            image_ref="launchbox-myapp:abc1234",
            port=3000,
            env_vars={},
            resource_limits={},
            health_check=None,
            app_name="myapp",
        )


# ----------------------------------------------------------------- wait_for_health


def test_wait_for_health_returns_true_when_probe_goes_healthy(mocker):
    mocker.patch(
        "launchbox.runner.docker_health_status",
        side_effect=["starting", "starting", "healthy"],
    )
    mocker.patch("launchbox.runner.container_is_running", return_value=True)
    mocker.patch("launchbox.runner.time.sleep")

    assert runner.wait_for_health(
        "myapp-abc1234", {"interval": "1s", "timeout": "1s", "retries": 3},
        poll_interval=0,
    ) is True


def test_wait_for_health_returns_false_when_probe_goes_unhealthy(mocker):
    mocker.patch(
        "launchbox.runner.docker_health_status",
        side_effect=["starting", "unhealthy"],
    )
    mocker.patch("launchbox.runner.container_is_running", return_value=True)
    mocker.patch("launchbox.runner.time.sleep")

    assert runner.wait_for_health(
        "myapp-abc1234", {"interval": "1s", "timeout": "1s", "retries": 1},
        poll_interval=0,
    ) is False


def test_wait_for_health_returns_false_when_container_disappears(mocker):
    mocker.patch("launchbox.runner.docker_health_status", return_value="missing")
    mocker.patch("launchbox.runner.container_is_running", return_value=False)
    mocker.patch("launchbox.runner.time.sleep")

    assert runner.wait_for_health(
        "myapp-abc1234", {"interval": "1s", "timeout": "1s", "retries": 1},
        poll_interval=0,
    ) is False


def test_wait_for_health_times_out_if_never_healthy(mocker):
    mocker.patch("launchbox.runner.docker_health_status", return_value="starting")
    mocker.patch("launchbox.runner.container_is_running", return_value=True)
    mocker.patch("launchbox.runner.time.sleep")

    assert runner.wait_for_health(
        "myapp-abc1234",
        {"interval": "1s", "timeout": "1s", "retries": 1},
        timeout=0.01,
        poll_interval=0,
    ) is False


def test_wait_for_health_without_probe_uses_settle_check(mocker):
    running = mocker.patch("launchbox.runner.container_is_running", return_value=True)
    mocker.patch("launchbox.runner.time.sleep")

    assert runner.wait_for_health("myapp-abc1234", None, settle_seconds=0) is True
    assert running.call_count >= 2, "must re-check after the settle period"


def test_wait_for_health_without_probe_fails_if_container_exits(mocker):
    mocker.patch(
        "launchbox.runner.container_is_running", side_effect=[True, False]
    )
    mocker.patch("launchbox.runner.time.sleep")

    assert runner.wait_for_health("myapp-abc1234", None, settle_seconds=0) is False


# ---------------------------------------------------------------- stop_and_remove


def test_stop_and_remove_returns_false_when_absent(mocker):
    mocker.patch("launchbox.runner.container_exists", return_value=False)
    run = mocker.patch("launchbox.runner.subprocess.run")

    assert runner.stop_and_remove("ghost") is False
    run.assert_not_called()


def test_stop_and_remove_removes_existing_container(mocker):
    mocker.patch("launchbox.runner.container_exists", return_value=True)
    run = mocker.patch(
        "launchbox.runner.subprocess.run", return_value=_completed()
    )

    assert runner.stop_and_remove("myapp-abc1234") is True
    cmd = run.call_args[0][0]
    assert cmd[:3] == ["docker", "rm", "-f"]
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `.venv/bin/pytest tests/test_runner.py -v`
Expected: FAIL — `AttributeError: module 'launchbox.runner' has no attribute 'parse_duration'`.

- [ ] **Step 3: Rewrite runner.py**

Replace the whole of `launchbox/runner.py`:

```python
"""Container lifecycle primitives.

This module deliberately contains no policy. It knows how to create, probe,
inspect and destroy a container; the decision about *when* to do each of those
belongs to ``launchbox.deploy``, which is what makes a health-gated swap
possible.
"""

import os
import re
import subprocess
import sys
import time
from typing import Any, Dict, Optional

from launchbox.logger import setup_logger, DeploymentError

logger = setup_logger("runner")

NETWORK = "traefik_default"

_DURATION_PATTERN = re.compile(r"^\s*([0-9]*\.?[0-9]+)\s*(ms|s|m|h)?\s*$", re.I)
_UNIT_SECONDS = {"ms": 0.001, "s": 1.0, "m": 60.0, "h": 3600.0}

DEFAULT_HEALTH_TIMEOUT = 120.0
HEALTH_GRACE_SECONDS = 15.0


# ------------------------------------------------------------------- durations


def parse_duration(value: Any, default: float = 0.0) -> float:
    """Parse a Docker/Traefik style duration such as ``30s`` into seconds."""
    if value is None:
        return default
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)

    match = _DURATION_PATTERN.match(str(value))
    if not match:
        return default

    magnitude = float(match.group(1))
    unit = (match.group(2) or "s").lower()
    return magnitude * _UNIT_SECONDS[unit]


def compute_health_timeout(health_check: Optional[Dict[str, Any]]) -> float:
    """How long to wait for a container to report healthy.

    Derived from the probe's own schedule so that a slow-starting application
    with a generous retry count is not cut off early. Overridable with
    ``LAUNCHBOX_HEALTH_TIMEOUT`` (seconds), which the test suite uses to keep
    integration runs quick.
    """
    override = os.environ.get("LAUNCHBOX_HEALTH_TIMEOUT")
    if override:
        return parse_duration(override, default=DEFAULT_HEALTH_TIMEOUT)

    if not health_check:
        return DEFAULT_HEALTH_TIMEOUT

    interval = parse_duration(health_check.get("interval"), default=30.0)
    probe_timeout = parse_duration(health_check.get("timeout"), default=10.0)
    try:
        retries = int(health_check.get("retries", 3))
    except (TypeError, ValueError):
        retries = 3

    return interval * max(retries, 1) + probe_timeout + HEALTH_GRACE_SECONDS


# ---------------------------------------------------------------- docker facts


def _docker(args, **kwargs) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["docker", *args], capture_output=True, text=True, check=False, **kwargs
    )


def ensure_network(network: str = NETWORK) -> None:
    """Create the shared network if it does not already exist."""
    if _docker(["network", "inspect", network]).returncode == 0:
        return
    logger.info(f"Creating Docker network: {network}")
    result = _docker(["network", "create", network])
    if result.returncode != 0 and "already exists" not in result.stderr:
        raise DeploymentError(f"Could not create network {network}: {result.stderr}")


def container_exists(container_name: str) -> bool:
    return _docker(["container", "inspect", container_name]).returncode == 0


def container_is_running(container_name: str) -> bool:
    result = _docker(
        ["container", "inspect", "-f", "{{.State.Running}}", container_name]
    )
    return result.returncode == 0 and result.stdout.strip() == "true"


def image_exists(image_ref: str) -> bool:
    return _docker(["image", "inspect", image_ref]).returncode == 0


def docker_health_status(container_name: str) -> str:
    """Return healthy | unhealthy | starting | none | missing."""
    result = _docker([
        "container", "inspect", "-f",
        "{{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}}",
        container_name,
    ])
    if result.returncode != 0:
        return "missing"
    return result.stdout.strip() or "none"


def container_logs(container_name: str, tail: int = 200) -> str:
    result = _docker(["logs", "--tail", str(tail), container_name])
    return (result.stdout or "") + (result.stderr or "")


def stop_and_remove(container_name: str) -> bool:
    """Force-remove a container. Returns True if one was removed."""
    if not container_exists(container_name):
        return False
    result = _docker(["rm", "-f", container_name])
    if result.returncode != 0:
        logger.warning(f"Failed to remove {container_name}: {result.stderr.strip()}")
        return False
    logger.info(f"Removed container: {container_name}")
    return True


def restart_container(container_name: str) -> bool:
    result = _docker(["restart", container_name])
    if result.returncode != 0:
        logger.warning(f"Failed to restart {container_name}: {result.stderr.strip()}")
        return False
    logger.info(f"Restarted container: {container_name}")
    return True


# ------------------------------------------------------------------- lifecycle


def build_health_command(port: int, path: str) -> str:
    """A probe that needs nothing but Python inside the image.

    ``curl`` is absent from most slim base images, so the probe is expressed as
    a one-line urllib call instead.
    """
    return (
        'python3 -c "import urllib.request; '
        f"urllib.request.urlopen('http://localhost:{port}{path}', timeout=5)\""
    )


def create_container(
    container_name: str,
    image_ref: str,
    port: int,
    env_vars: Dict[str, str],
    resource_limits: Dict[str, str],
    health_check: Optional[Dict[str, Any]],
    app_name: str,
    commit_sha: Optional[str] = None,
    network: str = NETWORK,
) -> str:
    """Create and start a container, without registering any route for it.

    The absence of Traefik labels is the point: the container is unreachable
    from outside until ``launchbox.router`` writes its route, which the
    orchestrator does only after the health probe succeeds.
    """
    ensure_network(network)

    cmd = [
        "docker", "run", "-d",
        "--name", container_name,
        "--network", network,
        "--restart", "unless-stopped",
        "--label", f"launchbox.app={app_name}",
        "--label", "launchbox.service=app",
    ]
    if commit_sha:
        cmd.extend(["--label", f"launchbox.commit={commit_sha}"])

    for key, value in env_vars.items():
        cmd.extend(["-e", f"{key}={value}"])

    for key, value in resource_limits.items():
        cmd.extend([f"--{key}", str(value)])

    if health_check:
        path = health_check.get("path", "/health")
        cmd.extend([
            "--health-cmd", build_health_command(port, path),
            "--health-interval", str(health_check.get("interval", "30s")),
            "--health-timeout", str(health_check.get("timeout", "10s")),
            "--health-retries", str(health_check.get("retries", 3)),
        ])

    cmd.append(image_ref)

    logger.info(f"Creating container {container_name} from {image_ref}")
    logger.debug(f"Running command: {' '.join(cmd)}")

    result = subprocess.run(cmd, capture_output=True, text=True, check=False)
    if result.returncode != 0:
        raise DeploymentError(f"Docker run failed: {result.stderr.strip()}")

    container_id = result.stdout.strip()
    logger.info(f"Started {container_name} ({container_id[:12]})")
    return container_id


def wait_for_health(
    container_name: str,
    health_check: Optional[Dict[str, Any]],
    timeout: Optional[float] = None,
    settle_seconds: float = 5.0,
    poll_interval: float = 1.0,
) -> bool:
    """Block until the container is verified healthy, or give up.

    With a configured probe this reads Docker's own health status. Without one
    it falls back to a weaker gate -- the container must be running, and still
    running after a settle period. That is weaker than a real probe but still
    catches the common case of an image that starts and immediately exits.
    """
    if not health_check:
        if not container_is_running(container_name):
            logger.warning(f"{container_name} is not running")
            return False
        time.sleep(settle_seconds)
        alive = container_is_running(container_name)
        if not alive:
            logger.warning(f"{container_name} exited during the settle period")
        return alive

    if timeout is None:
        timeout = compute_health_timeout(health_check)

    logger.info(f"Waiting up to {timeout:.0f}s for {container_name} to report healthy")
    deadline = time.monotonic() + timeout

    while time.monotonic() < deadline:
        status = docker_health_status(container_name)

        if status == "healthy":
            logger.info(f"{container_name} is healthy")
            return True
        if status == "unhealthy":
            logger.warning(f"{container_name} reported unhealthy")
            return False
        if status == "missing":
            logger.warning(f"{container_name} disappeared during the health probe")
            return False
        if status == "none":
            # Image declares no HEALTHCHECK and none was injected.
            return container_is_running(container_name)
        if not container_is_running(container_name):
            logger.warning(f"{container_name} stopped during the health probe")
            return False

        time.sleep(poll_interval)

    logger.warning(f"{container_name} did not become healthy within {timeout:.0f}s")
    return False


def run(app_name: str) -> bool:
    """Backwards-compatible entry point.

    ``dashboard.py`` still calls ``run(app_name)``. Delegate to the orchestrator
    so both paths share one implementation.
    """
    from launchbox.deploy import deploy

    deploy(app_name)
    return True


if __name__ == "__main__":
    if len(sys.argv) < 2:
        logger.error("Usage: python3 -m launchbox.runner <app_name>")
        sys.exit(1)

    try:
        run(sys.argv[1])
        sys.exit(0)
    except DeploymentError as exc:
        logger.error(f"Deployment failed: {exc}")
        sys.exit(1)
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `.venv/bin/pytest tests/test_runner.py -v`
Expected: all tests PASS.

Note: `test_runner.py` does not import `launchbox.deploy`, and `runner.run()`
imports it lazily inside the function body, so these tests pass before Task 6
exists.

- [ ] **Step 5: Commit**

```bash
git add launchbox/runner.py tests/test_runner.py
git commit -m "refactor: split runner into container lifecycle primitives

Creates containers with no Traefik labels and a restart policy, and adds
health probing, log capture and removal as separate operations. Policy
about when to invoke each moves to the orchestrator, which is what allows
a container to be verified before it receives traffic.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```

---

### Task 6: Deployment orchestrator with the health gate

**Files:**
- Create: `launchbox/deploy.py`
- Test: `tests/test_deploy.py`

**Interfaces:**
- Consumes: `state.StateStore`, `router.write_route`, `builder.build`, all of the
  `runner` primitives from Task 5, `LaunchboxConfig`.
- Produces:

```python
deploy(app_name: str, source_dir: str | None = None,
       commit_sha: str | None = None,
       store: StateStore | None = None) -> str    # the new container name
```

Raises `DeploymentError` on any failure. `store` is injectable for testing;
when omitted a `StateStore()` is created and closed by the function.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_deploy.py`:

```python
import os

import pytest

from launchbox.logger import DeploymentError
from launchbox.state import StateStore


@pytest.fixture
def app_dir(tmp_path):
    d = tmp_path / "myapp"
    d.mkdir()
    (d / "Dockerfile").write_text("FROM scratch\n")
    (d / "launchbox.yaml").write_text(
        "app:\n"
        "  port: 3000\n"
        "  health_check:\n"
        "    path: /health\n"
        "    interval: 1s\n"
        "    timeout: 1s\n"
        "    retries: 2\n"
    )
    return str(d)


@pytest.fixture
def store(tmp_state_db):
    s = StateStore(tmp_state_db)
    yield s
    s.close()


@pytest.fixture
def fake_docker(mocker, tmp_dynamic_dir):
    """Patch every outside-world call the orchestrator makes."""
    from launchbox import deploy as deploy_module

    mocker.patch.object(deploy_module.router, "DYNAMIC_DIR", tmp_dynamic_dir)
    return {
        "build": mocker.patch.object(
            deploy_module.builder, "build",
            return_value="launchbox-myapp:abc1234",
        ),
        "create": mocker.patch.object(
            deploy_module.runner, "create_container", return_value="cid123"
        ),
        "health": mocker.patch.object(
            deploy_module.runner, "wait_for_health", return_value=True
        ),
        "remove": mocker.patch.object(
            deploy_module.runner, "stop_and_remove", return_value=True
        ),
        "logs": mocker.patch.object(
            deploy_module.runner, "container_logs", return_value="crash trace"
        ),
        "exists": mocker.patch.object(
            deploy_module.runner, "container_exists", return_value=False
        ),
        "write_route": mocker.patch.object(
            deploy_module.router, "write_route",
            return_value=os.path.join(tmp_dynamic_dir, "myapp.yml"),
        ),
        "dynamic_dir": tmp_dynamic_dir,
    }


def test_successful_deploy_names_container_by_short_sha(app_dir, store, fake_docker):
    from launchbox.deploy import deploy

    name = deploy("myapp", source_dir=app_dir, commit_sha="abc1234deadbeef",
                  store=store)

    assert name == "myapp-abc1234"
    assert store.current_container("myapp") == "myapp-abc1234"


def test_successful_deploy_writes_the_route_after_the_probe(app_dir, store,
                                                            fake_docker):
    from launchbox.deploy import deploy

    call_order = []
    fake_docker["health"].side_effect = lambda *a, **k: (
        call_order.append("probe") or True
    )
    fake_docker["write_route"].side_effect = lambda *a, **k: (
        call_order.append("route") or "path"
    )

    deploy("myapp", source_dir=app_dir, commit_sha="abc1234", store=store)

    assert call_order == ["probe", "route"], (
        "the route must not be registered before the container is verified"
    )


def test_successful_deploy_records_history(app_dir, store, fake_docker):
    from launchbox.deploy import deploy

    deploy("myapp", source_dir=app_dir, commit_sha="abc1234", store=store)

    history = store.list_deployments("myapp")
    assert len(history) == 1
    assert history[0]["status"] == "success"
    assert history[0]["image_tag"] == "launchbox-myapp:abc1234"


def test_failed_health_probe_leaves_the_old_container_running(app_dir, store,
                                                              fake_docker):
    """The headline guarantee of this whole plan."""
    from launchbox.deploy import deploy

    # An earlier successful deployment is currently serving.
    previous = store.record_start("myapp", "1111111")
    store.record_success(previous, "myapp-1111111", "launchbox-myapp:1111111")

    fake_docker["health"].return_value = False

    with pytest.raises(DeploymentError, match="health"):
        deploy("myapp", source_dir=app_dir, commit_sha="2222222", store=store)

    # The old container is untouched...
    removed = [c[0][0] for c in fake_docker["remove"].call_args_list]
    assert "myapp-1111111" not in removed
    # ...only the unverified new one was cleaned up...
    assert "myapp-2222222" in removed
    # ...the route never moved...
    fake_docker["write_route"].assert_not_called()
    # ...and the app still points at the old container.
    assert store.current_container("myapp") == "myapp-1111111"


def test_failed_health_probe_captures_logs_into_history(app_dir, store,
                                                        fake_docker):
    from launchbox.deploy import deploy

    fake_docker["health"].return_value = False

    with pytest.raises(DeploymentError):
        deploy("myapp", source_dir=app_dir, commit_sha="2222222", store=store)

    record = store.list_deployments("myapp")[0]
    assert record["status"] == "failed"
    assert record["logs"] == "crash trace"


def test_failed_build_never_touches_the_running_container(app_dir, store,
                                                          fake_docker):
    from launchbox.deploy import deploy
    from launchbox.logger import BuildError

    previous = store.record_start("myapp", "1111111")
    store.record_success(previous, "myapp-1111111", "launchbox-myapp:1111111")

    fake_docker["build"].side_effect = BuildError("compile error")

    with pytest.raises(DeploymentError, match="compile error"):
        deploy("myapp", source_dir=app_dir, commit_sha="2222222", store=store)

    fake_docker["create"].assert_not_called()
    fake_docker["remove"].assert_not_called()
    fake_docker["write_route"].assert_not_called()
    assert store.current_container("myapp") == "myapp-1111111"
    assert store.list_deployments("myapp")[0]["status"] == "failed"


def test_container_creation_failure_leaves_old_container_running(app_dir, store,
                                                                 fake_docker):
    from launchbox.deploy import deploy

    previous = store.record_start("myapp", "1111111")
    store.record_success(previous, "myapp-1111111", "launchbox-myapp:1111111")

    fake_docker["create"].side_effect = DeploymentError("port clash")

    with pytest.raises(DeploymentError):
        deploy("myapp", source_dir=app_dir, commit_sha="2222222", store=store)

    removed = [c[0][0] for c in fake_docker["remove"].call_args_list]
    assert "myapp-1111111" not in removed
    fake_docker["write_route"].assert_not_called()


def test_successful_redeploy_removes_the_previous_container(app_dir, store,
                                                            fake_docker):
    from launchbox.deploy import deploy

    previous = store.record_start("myapp", "1111111")
    store.record_success(previous, "myapp-1111111", "launchbox-myapp:1111111")

    deploy("myapp", source_dir=app_dir, commit_sha="2222222", store=store)

    removed = [c[0][0] for c in fake_docker["remove"].call_args_list]
    assert "myapp-1111111" in removed
    assert store.current_container("myapp") == "myapp-2222222"


def test_legacy_container_named_after_the_app_is_adopted(app_dir, store,
                                                         fake_docker):
    """Migration from the pre-swap naming scheme."""
    from launchbox.deploy import deploy

    # No state row exists, but a container named plain "myapp" is running.
    fake_docker["exists"].side_effect = lambda name: name == "myapp"

    deploy("myapp", source_dir=app_dir, commit_sha="2222222", store=store)

    removed = [c[0][0] for c in fake_docker["remove"].call_args_list]
    assert "myapp" in removed, "the legacy container must be cleaned up"


def test_deploy_passes_https_settings_to_the_router(tmp_path, store, mocker,
                                                    tmp_dynamic_dir):
    from launchbox import deploy as deploy_module
    from launchbox.deploy import deploy

    d = tmp_path / "secureapp"
    d.mkdir()
    (d / "Dockerfile").write_text("FROM scratch\n")
    (d / "launchbox.yaml").write_text(
        "app:\n  port: 8080\nhttps:\n  enabled: true\n  redirect_http: true\n"
    )

    mocker.patch.object(deploy_module.builder, "build",
                        return_value="launchbox-secureapp:abc1234")
    mocker.patch.object(deploy_module.runner, "create_container",
                        return_value="cid")
    mocker.patch.object(deploy_module.runner, "wait_for_health", return_value=True)
    mocker.patch.object(deploy_module.runner, "stop_and_remove", return_value=True)
    mocker.patch.object(deploy_module.runner, "container_exists", return_value=False)
    write_route = mocker.patch.object(deploy_module.router, "write_route",
                                      return_value="path")

    deploy("secureapp", source_dir=str(d), commit_sha="abc1234", store=store)

    kwargs = write_route.call_args.kwargs
    assert kwargs["https"] is True
    assert kwargs["redirect_http"] is True
    assert write_route.call_args.args[2] == 8080


def test_deploy_skips_database_when_not_enabled(app_dir, store, fake_docker,
                                                mocker):
    """DatabaseManager must not even be constructed without a daemon."""
    from launchbox import deploy as deploy_module

    manager = mocker.patch.object(deploy_module, "DatabaseManager")

    deploy_module.deploy("myapp", source_dir=app_dir, commit_sha="abc1234",
                         store=store)

    manager.assert_not_called()


def test_deploy_injects_database_env_when_enabled(tmp_path, store, mocker,
                                                  tmp_dynamic_dir):
    from launchbox import deploy as deploy_module

    d = tmp_path / "dbapp"
    d.mkdir()
    (d / "Dockerfile").write_text("FROM scratch\n")
    (d / "launchbox.yaml").write_text(
        "app:\n  port: 3000\ndatabase:\n  enabled: true\n  type: postgresql\n"
    )

    mocker.patch.object(deploy_module.builder, "build",
                        return_value="launchbox-dbapp:abc1234")
    create = mocker.patch.object(deploy_module.runner, "create_container",
                                 return_value="cid")
    mocker.patch.object(deploy_module.runner, "wait_for_health", return_value=True)
    mocker.patch.object(deploy_module.runner, "stop_and_remove", return_value=True)
    mocker.patch.object(deploy_module.runner, "container_exists", return_value=False)
    mocker.patch.object(deploy_module.router, "write_route", return_value="path")

    manager = mocker.patch.object(deploy_module, "DatabaseManager")
    manager.return_value.create_database_for_app.return_value = {
        "DATABASE_URL": "postgresql://u:p@dbapp_postgres:5432/dbapp_db"
    }

    deploy_module.deploy("dbapp", source_dir=str(d), commit_sha="abc1234",
                         store=store)

    env = create.call_args.kwargs["env_vars"]
    assert env["DATABASE_URL"] == "postgresql://u:p@dbapp_postgres:5432/dbapp_db"


def test_database_failure_leaves_old_container_running(tmp_path, store, mocker,
                                                       tmp_dynamic_dir):
    from launchbox import deploy as deploy_module

    d = tmp_path / "dbapp"
    d.mkdir()
    (d / "Dockerfile").write_text("FROM scratch\n")
    (d / "launchbox.yaml").write_text(
        "app:\n  port: 3000\ndatabase:\n  enabled: true\n  type: postgresql\n"
    )

    previous = store.record_start("dbapp", "1111111")
    store.record_success(previous, "dbapp-1111111", "launchbox-dbapp:1111111")

    mocker.patch.object(deploy_module.builder, "build",
                        return_value="launchbox-dbapp:2222222")
    create = mocker.patch.object(deploy_module.runner, "create_container")
    remove = mocker.patch.object(deploy_module.runner, "stop_and_remove")
    mocker.patch.object(deploy_module.runner, "container_exists", return_value=False)
    write_route = mocker.patch.object(deploy_module.router, "write_route")

    manager = mocker.patch.object(deploy_module, "DatabaseManager")
    manager.return_value.create_database_for_app.side_effect = RuntimeError(
        "postgres never became ready"
    )

    with pytest.raises(DeploymentError, match="postgres never became ready"):
        deploy_module.deploy("dbapp", source_dir=str(d), commit_sha="2222222",
                             store=store)

    create.assert_not_called()
    remove.assert_not_called()
    write_route.assert_not_called()
    assert store.current_container("dbapp") == "dbapp-1111111"


def test_deploy_resets_supervisor_state_on_success(app_dir, store, fake_docker):
    from launchbox.deploy import deploy

    store.increment_restart_attempts("myapp")
    store.increment_restart_attempts("myapp")
    store.mark_failed("myapp")

    deploy("myapp", source_dir=app_dir, commit_sha="abc1234", store=store)

    assert store.restart_attempts("myapp") == 0
    assert store.is_marked_failed("myapp") is False
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `.venv/bin/pytest tests/test_deploy.py -v`
Expected: `ModuleNotFoundError: No module named 'launchbox.deploy'`.

- [ ] **Step 3: Write the implementation**

Create `launchbox/deploy.py`:

```python
"""Deployment orchestrator.

This is the component Chapter 3 of the dissertation designed and Chapter 4
records as missing: a single place that owns step ordering and error handling
for a deployment, so that a failure at any stage leaves the previously running
version serving traffic.

The ordering guarantee is simple and worth stating plainly. Nothing that
destroys the running version happens until the replacement has been created,
started and verified healthy. If any earlier step raises, the running container
and its route are untouched.
"""

import os
import time
from typing import Any, Dict, Optional

from launchbox import builder, router, runner
from launchbox.config import APPS_DIR
from launchbox.config_parser import LaunchboxConfig
from launchbox.database_manager import DatabaseManager
from launchbox.logger import setup_logger, DeploymentError, LaunchboxError
from launchbox.state import StateStore

logger = setup_logger("deploy")


def _short(commit_sha: Optional[str]) -> str:
    return commit_sha[:7] if commit_sha else "latest"


def _find_previous_container(app_name: str, store: StateStore) -> Optional[str]:
    """The container currently serving this app, if any.

    Falls back to the pre-swap naming scheme so the first deployment after this
    change migrates without manual intervention.
    """
    current = store.current_container(app_name)
    if current and runner.container_exists(current):
        return current
    if runner.container_exists(app_name):
        logger.info(f"Adopting legacy container named '{app_name}'")
        return app_name
    return current


def _collect_environment(
    app_name: str, source_dir: str, config: LaunchboxConfig
) -> Dict[str, str]:
    env_vars = dict(config.get_environment_vars())

    if config.is_database_enabled():
        manager = DatabaseManager()
        connection_info = manager.create_database_for_app(app_name, source_dir)
        if connection_info:
            logger.info(f"Database provisioned for {app_name}")
            env_vars.update(connection_info)

    return {str(k): str(v) for k, v in env_vars.items()}


def _promote(
    app_name: str,
    container_name: str,
    image_ref: str,
    config: LaunchboxConfig,
    env_vars: Dict[str, str],
    commit_sha: Optional[str],
    previous_container: Optional[str],
    store: StateStore,
    deployment_id: int,
) -> str:
    """Create, verify, then route. Shared by deploy and rollback."""
    # A stale container under the same name would block creation; it can only
    # exist if a previous attempt died between creation and cleanup.
    if container_name != previous_container:
        runner.stop_and_remove(container_name)

    runner.create_container(
        container_name=container_name,
        image_ref=image_ref,
        port=config.get_port(),
        env_vars=env_vars,
        resource_limits=config.get_resource_limits(),
        health_check=config.get_health_check(),
        app_name=app_name,
        commit_sha=commit_sha,
    )

    healthy = runner.wait_for_health(container_name, config.get_health_check())

    if not healthy:
        logs = runner.container_logs(container_name)
        runner.stop_and_remove(container_name)
        store.record_failure(deployment_id, "health probe failed", logs)
        raise DeploymentError(
            f"{app_name}: health probe failed; previous version left running"
        )

    router.write_route(
        app_name,
        container_name,
        config.get_port(),
        https=config.is_https_enabled(),
        redirect_http=config.should_redirect_http(),
    )

    if previous_container and previous_container != container_name:
        runner.stop_and_remove(previous_container)

    store.record_success(deployment_id, container_name, image_ref)
    store.reset_restart_attempts(app_name)
    store.clear_failed(app_name)

    logger.info(f"{app_name} deployed: http://{app_name}.localhost")
    return container_name


def deploy(
    app_name: str,
    source_dir: Optional[str] = None,
    commit_sha: Optional[str] = None,
    store: Optional[StateStore] = None,
) -> str:
    """Build and deploy an application, returning the new container name.

    Args:
        app_name: the registered application.
        source_dir: what to build. Defaults to ``apps/<app_name>``. The git
            hook passes a temporary worktree holding the pushed commit.
        commit_sha: the commit being deployed; used for image and container
            naming and recorded in the deployment history.
        store: injectable state store, for testing.
    """
    build_source = source_dir or os.path.join(APPS_DIR, app_name)
    owns_store = store is None
    store = store or StateStore()

    deployment_id = store.record_start(app_name, commit_sha)
    logger.info(f"Deployment {deployment_id} started for {app_name}")

    try:
        config = LaunchboxConfig(build_source)

        image_ref = builder.build(app_name, source_dir=build_source,
                                  tag=_short(commit_sha))
        env_vars = _collect_environment(app_name, build_source, config)

        previous = _find_previous_container(app_name, store)
        container_name = f"{app_name}-{_short(commit_sha)}"
        if container_name == previous:
            container_name = f"{container_name}-{int(time.time())}"

        return _promote(
            app_name=app_name,
            container_name=container_name,
            image_ref=image_ref,
            config=config,
            env_vars=env_vars,
            commit_sha=commit_sha,
            previous_container=previous,
            store=store,
            deployment_id=deployment_id,
        )

    except DeploymentError:
        raise
    except LaunchboxError as exc:
        store.record_failure(deployment_id, str(exc))
        logger.error(f"Deployment {deployment_id} failed: {exc}")
        raise DeploymentError(str(exc)) from exc
    except Exception as exc:
        store.record_failure(deployment_id, str(exc))
        logger.error(f"Deployment {deployment_id} failed unexpectedly: {exc}")
        raise DeploymentError(str(exc)) from exc
    finally:
        if owns_store:
            store.close()
```

Note on the `except DeploymentError: raise` branch: `_promote` already records
the failure with the captured container logs before raising, so re-recording
here would overwrite the logs with a bare message.

- [ ] **Step 4: Run the tests to verify they pass**

Run: `.venv/bin/pytest tests/test_deploy.py -v`
Expected: all tests PASS.

- [ ] **Step 5: Run the whole suite to check nothing regressed**

Run: `.venv/bin/pytest -v`
Expected: all tests PASS.

- [ ] **Step 6: Commit**

```bash
git add launchbox/deploy.py tests/test_deploy.py
git commit -m "feat: health-gated deployment swap

The replacement container is created, started and verified healthy before
its route is written and before the previous container is touched. A
failed build, a failed database provision, a container that will not start
and a container that fails its probe all now leave the running version
serving traffic, and are recorded as failed deployments.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```

---

### Task 7: Git hook that deploys the pushed commit

**Files:**
- Modify: `launchbox/init.py`
- Test: `tests/test_hook.py`

**Interfaces:**
- Consumes: `launchbox.config.REPOS_DIR`, `BASE_DIR`.
- Produces:

```python
render_hook(app_name: str, base_dir: str, default_branch: str = "main") -> str
init(app_name: str, default_branch: str = "main") -> str   # repo path
```

The generated hook reads `oldrev newrev refname` triples from stdin, ignores
pushes to anything but the default branch, checks the pushed commit into a
temporary worktree, and invokes the orchestrator once.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_hook.py`:

```python
import os
import stat
import subprocess

import pytest

from launchbox import init as init_module


def test_hook_sets_strict_bash_flags():
    hook = init_module.render_hook("myapp", "/opt/launchbox")
    assert hook.startswith("#!/bin/bash")
    assert "set -euo pipefail" in hook


def test_hook_reads_the_push_from_stdin():
    hook = init_module.render_hook("myapp", "/opt/launchbox")
    assert "while read -r oldrev newrev refname" in hook


def test_hook_filters_to_the_default_branch():
    hook = init_module.render_hook("myapp", "/opt/launchbox", default_branch="main")
    assert 'refs/heads/main' in hook
    assert "Ignoring push" in hook


def test_hook_honours_a_custom_default_branch():
    hook = init_module.render_hook("myapp", "/opt/launchbox",
                                   default_branch="production")
    assert "refs/heads/production" in hook


def test_hook_checks_out_into_a_temporary_worktree():
    hook = init_module.render_hook("myapp", "/opt/launchbox")
    assert "mktemp -d" in hook
    assert "--work-tree=" in hook
    assert 'checkout -f "$newrev"' in hook
    assert "rm -rf" in hook, "the temporary worktree must be cleaned up"


def test_hook_calls_the_orchestrator_with_source_and_commit():
    hook = init_module.render_hook("myapp", "/opt/launchbox")
    assert "-m launchbox deploy" in hook
    assert "--source" in hook
    assert "--commit" in hook


def test_hook_does_not_call_builder_and_runner_separately():
    """The two-invocation pipeline is what allowed a failed build through."""
    hook = init_module.render_hook("myapp", "/opt/launchbox")
    assert "launchbox.builder" not in hook
    assert "launchbox.runner" not in hook


def test_hook_prefers_the_project_virtualenv():
    hook = init_module.render_hook("myapp", "/opt/launchbox")
    assert "/opt/launchbox/.venv/bin/python" in hook


def test_hook_is_valid_bash():
    hook = init_module.render_hook("myapp", "/opt/launchbox")
    result = subprocess.run(
        ["bash", "-n"], input=hook, text=True, capture_output=True, check=False
    )
    assert result.returncode == 0, result.stderr


def test_init_creates_a_bare_repo_with_an_executable_hook(tmp_path, monkeypatch):
    repos = tmp_path / "repos"
    repos.mkdir()
    monkeypatch.setattr(init_module, "REPOS_DIR", str(repos))
    monkeypatch.setattr(init_module, "BASE_DIR", str(tmp_path))

    repo_path = init_module.init("myapp")

    assert os.path.isdir(repo_path)
    assert repo_path == str(repos / "myapp.git")
    # A bare repository has no working tree.
    assert os.path.exists(os.path.join(repo_path, "HEAD"))

    hook_path = os.path.join(repo_path, "hooks", "post-receive")
    assert os.path.exists(hook_path)
    assert os.stat(hook_path).st_mode & stat.S_IXUSR


def test_init_is_idempotent(tmp_path, monkeypatch):
    repos = tmp_path / "repos"
    repos.mkdir()
    monkeypatch.setattr(init_module, "REPOS_DIR", str(repos))
    monkeypatch.setattr(init_module, "BASE_DIR", str(tmp_path))

    first = init_module.init("myapp")
    second = init_module.init("myapp")

    assert first == second


def test_init_rewrites_the_hook_on_reinitialisation(tmp_path, monkeypatch):
    """Existing apps must pick up the new hook without recreating the repo."""
    repos = tmp_path / "repos"
    repos.mkdir()
    monkeypatch.setattr(init_module, "REPOS_DIR", str(repos))
    monkeypatch.setattr(init_module, "BASE_DIR", str(tmp_path))

    repo_path = init_module.init("myapp")
    hook_path = os.path.join(repo_path, "hooks", "post-receive")
    with open(hook_path, "w") as handle:
        handle.write("#!/bin/bash\necho stale\n")

    init_module.init("myapp")

    with open(hook_path) as handle:
        assert "launchbox deploy" in handle.read()
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `.venv/bin/pytest tests/test_hook.py -v`
Expected: `AttributeError: module 'launchbox.init' has no attribute 'render_hook'`.

- [ ] **Step 3: Rewrite init.py**

Replace the whole of `launchbox/init.py`:

```python
"""Application registration.

Creates the bare repository that receives pushes and installs the post-receive
hook that triggers a deployment.
"""

import os
import subprocess
import sys

from launchbox.config import BASE_DIR, REPOS_DIR
from launchbox.logger import setup_logger, LaunchboxError

logger = setup_logger("init")

HOOK_TEMPLATE = """#!/bin/bash
# Launchbox post-receive hook -- generated, do not edit by hand.
set -euo pipefail

APP_NAME="{app_name}"
BASE_DIR="{base_dir}"
DEFAULT_BRANCH="{default_branch}"

if [ -x "$BASE_DIR/.venv/bin/python" ]; then
    PYTHON_BIN="$BASE_DIR/.venv/bin/python"
else
    PYTHON_BIN="python3"
fi

GIT_REPO_DIR="$(git rev-parse --absolute-git-dir)"

while read -r oldrev newrev refname; do
    if [ "$refname" != "refs/heads/$DEFAULT_BRANCH" ]; then
        echo "[Launchbox] Ignoring push to $refname"
        continue
    fi

    echo "[Launchbox] Deploying $APP_NAME @ ${{newrev:0:7}}"

    WORKDIR="$(mktemp -d)"
    trap 'rm -rf "$WORKDIR"' EXIT

    git --git-dir="$GIT_REPO_DIR" --work-tree="$WORKDIR" \\
        checkout -f "$newrev" -- .

    cd "$BASE_DIR"
    PYTHONPATH="$BASE_DIR" "$PYTHON_BIN" -m launchbox deploy "$APP_NAME" \\
        --source "$WORKDIR" --commit "$newrev"

    rm -rf "$WORKDIR"
    trap - EXIT
done
"""


def render_hook(app_name: str, base_dir: str, default_branch: str = "main") -> str:
    """Render the post-receive hook for an application.

    Three properties matter, and each fixes a defect in the previous hook:

    * ``set -euo pipefail`` means a failing deployment fails the push visibly,
      rather than being swallowed.
    * The branch filter stops a push to a feature branch deploying to
      production.
    * The temporary worktree means what is built is the commit that was
      actually pushed, rather than whatever happens to be sitting on disk.
    """
    return HOOK_TEMPLATE.format(
        app_name=app_name,
        base_dir=base_dir,
        default_branch=default_branch,
    )


def init(app_name: str, default_branch: str = "main") -> str:
    """Register an application. Returns the bare repository path.

    Safe to re-run: an existing repository is kept and only its hook is
    refreshed, so applications registered under an older Launchbox pick up the
    current pipeline.
    """
    repo_path = os.path.join(REPOS_DIR, f"{app_name}.git")
    hooks_dir = os.path.join(repo_path, "hooks")
    hook_path = os.path.join(hooks_dir, "post-receive")

    if os.path.isdir(repo_path):
        logger.info(f"Repository already exists: {repo_path}")
    else:
        os.makedirs(REPOS_DIR, exist_ok=True)
        result = subprocess.run(
            ["git", "init", "--bare", repo_path],
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode != 0:
            raise LaunchboxError(f"git init --bare failed: {result.stderr.strip()}")
        logger.info(f"Created bare repo at {repo_path}")

    os.makedirs(hooks_dir, exist_ok=True)
    with open(hook_path, "w") as handle:
        handle.write(render_hook(app_name, BASE_DIR, default_branch))
    os.chmod(hook_path, 0o755)
    logger.info(f"Hook installed at {hook_path}")

    app_dir = os.path.join(BASE_DIR, "apps", app_name)
    if not os.path.isdir(app_dir):
        logger.warning(
            f"No application directory at {app_dir}. "
            "Create it with a Dockerfile before pushing."
        )

    return repo_path


if __name__ == "__main__":
    if len(sys.argv) < 2:
        logger.error("Usage: python3 -m launchbox.init <app_name>")
        sys.exit(1)
    init(sys.argv[1])
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `.venv/bin/pytest tests/test_hook.py -v`
Expected: all tests PASS.

- [ ] **Step 5: Commit**

```bash
git add launchbox/init.py tests/test_hook.py
git commit -m "feat: deploy the commit that was actually pushed

The post-receive hook now reads the push from stdin, ignores non-default
branches, checks the pushed commit into a temporary worktree and calls the
orchestrator once. set -euo pipefail means a failed deployment fails the
push visibly instead of being swallowed between two invocations.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```

---

### Task 8: Rollback and the unified CLI

**Files:**
- Modify: `launchbox/deploy.py`
- Create: `launchbox/__main__.py`
- Test: `tests/test_rollback.py`

**Interfaces:**
- Consumes: everything from Tasks 2–7.
- Produces:

```python
# launchbox/deploy.py
rollback(app_name: str, commit_sha: str | None = None,
         store: StateStore | None = None) -> str   # the restored container name
```

```
# CLI, all via .venv/bin/python -m launchbox <command>
init <app> [--branch main]
build <app> [--source DIR] [--tag TAG]
deploy <app> [--source DIR] [--commit SHA]
rollback <app> [--commit SHA]
history <app> [--limit N]
list
logs <app> [--tail N]
dashboard
supervisor [--interval SECONDS]
```

- [ ] **Step 1: Write the failing tests**

Create `tests/test_rollback.py`:

```python
import pytest

from launchbox.logger import DeploymentError
from launchbox.state import StateStore


@pytest.fixture
def app_dir(tmp_path):
    d = tmp_path / "myapp"
    d.mkdir()
    (d / "Dockerfile").write_text("FROM scratch\n")
    (d / "launchbox.yaml").write_text("app:\n  port: 3000\n")
    return str(d)


@pytest.fixture
def store(tmp_state_db):
    s = StateStore(tmp_state_db)
    yield s
    s.close()


@pytest.fixture
def two_deployments(store):
    """An older good version and a newer one currently serving."""
    old = store.record_start("myapp", "1111111")
    store.record_success(old, "myapp-1111111", "launchbox-myapp:1111111")
    new = store.record_start("myapp", "2222222")
    store.record_success(new, "myapp-2222222", "launchbox-myapp:2222222")
    return old, new


@pytest.fixture
def fake_docker(mocker, app_dir, monkeypatch):
    import os

    from launchbox import deploy as deploy_module

    # rollback reads config from apps/<app>, not from a pushed worktree, so
    # point APPS_DIR at the parent of the fixture's application directory.
    monkeypatch.setattr(deploy_module, "APPS_DIR", os.path.dirname(app_dir))
    return {
        "image_exists": mocker.patch.object(
            deploy_module.runner, "image_exists", return_value=True
        ),
        "create": mocker.patch.object(
            deploy_module.runner, "create_container", return_value="cid"
        ),
        "health": mocker.patch.object(
            deploy_module.runner, "wait_for_health", return_value=True
        ),
        "remove": mocker.patch.object(
            deploy_module.runner, "stop_and_remove", return_value=True
        ),
        "logs": mocker.patch.object(
            deploy_module.runner, "container_logs", return_value=""
        ),
        "exists": mocker.patch.object(
            deploy_module.runner, "container_exists", return_value=True
        ),
        "write_route": mocker.patch.object(
            deploy_module.router, "write_route", return_value="path"
        ),
        "build": mocker.patch.object(deploy_module.builder, "build"),
    }


def test_rollback_restores_the_previous_version(store, two_deployments,
                                                 fake_docker):
    from launchbox.deploy import rollback

    name = rollback("myapp", store=store)

    assert name.startswith("myapp-1111111")
    assert store.current_container("myapp") == name


def test_rollback_never_rebuilds(store, two_deployments, fake_docker):
    from launchbox.deploy import rollback

    rollback("myapp", store=store)

    fake_docker["build"].assert_not_called()


def test_rollback_reuses_the_recorded_image(store, two_deployments, fake_docker):
    from launchbox.deploy import rollback

    rollback("myapp", store=store)

    assert fake_docker["create"].call_args.kwargs["image_ref"] == (
        "launchbox-myapp:1111111"
    )


def test_rollback_removes_the_container_it_replaced(store, two_deployments,
                                                    fake_docker):
    from launchbox.deploy import rollback

    rollback("myapp", store=store)

    removed = [c[0][0] for c in fake_docker["remove"].call_args_list]
    assert "myapp-2222222" in removed


def test_rollback_to_an_explicit_sha(store, fake_docker):
    from launchbox.deploy import rollback

    for sha in ("1111111", "2222222", "3333333"):
        dep = store.record_start("myapp", sha)
        store.record_success(dep, f"myapp-{sha}", f"launchbox-myapp:{sha}")

    rollback("myapp", commit_sha="1111111", store=store)

    assert fake_docker["create"].call_args.kwargs["image_ref"] == (
        "launchbox-myapp:1111111"
    )


def test_rollback_fails_when_there_is_nothing_to_roll_back_to(store, fake_docker):
    from launchbox.deploy import rollback

    only = store.record_start("myapp", "1111111")
    store.record_success(only, "myapp-1111111", "launchbox-myapp:1111111")

    with pytest.raises(DeploymentError, match="no previous"):
        rollback("myapp", store=store)


def test_rollback_fails_when_the_image_is_gone(store, two_deployments,
                                               fake_docker):
    from launchbox.deploy import rollback

    fake_docker["image_exists"].return_value = False

    with pytest.raises(DeploymentError, match="image"):
        rollback("myapp", store=store)

    fake_docker["create"].assert_not_called()


def test_failed_rollback_probe_leaves_the_current_version_running(
    store, two_deployments, fake_docker
):
    from launchbox.deploy import rollback

    fake_docker["health"].return_value = False

    with pytest.raises(DeploymentError, match="health"):
        rollback("myapp", store=store)

    removed = [c[0][0] for c in fake_docker["remove"].call_args_list]
    assert "myapp-2222222" not in removed
    fake_docker["write_route"].assert_not_called()
    assert store.current_container("myapp") == "myapp-2222222"


def test_rollback_records_a_deployment_row(store, two_deployments, fake_docker):
    from launchbox.deploy import rollback

    rollback("myapp", store=store)

    history = store.list_deployments("myapp")
    assert history[0]["status"] == "success"
    assert history[0]["commit_sha"] == "1111111"
    assert len(history) == 3


def test_cli_exposes_the_expected_commands():
    from launchbox.__main__ import build_parser

    parser = build_parser()
    subparsers = [
        action for action in parser._actions
        if hasattr(action, "choices") and action.choices
    ]
    commands = set(subparsers[0].choices)

    assert {
        "init", "build", "deploy", "rollback",
        "history", "list", "logs", "dashboard", "supervisor",
    } <= commands


def test_cli_deploy_passes_source_and_commit(mocker):
    from launchbox import __main__ as cli

    deploy = mocker.patch.object(cli, "deploy", return_value="myapp-abc1234")

    exit_code = cli.main(
        ["deploy", "myapp", "--source", "/tmp/work", "--commit", "abc1234def"]
    )

    assert exit_code == 0
    deploy.assert_called_once_with(
        "myapp", source_dir="/tmp/work", commit_sha="abc1234def"
    )


def test_cli_returns_nonzero_on_deployment_error(mocker):
    from launchbox import __main__ as cli

    mocker.patch.object(cli, "deploy", side_effect=DeploymentError("boom"))

    assert cli.main(["deploy", "myapp"]) == 1
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `.venv/bin/pytest tests/test_rollback.py -v`
Expected: FAIL — `ImportError: cannot import name 'rollback' from 'launchbox.deploy'`.

- [ ] **Step 3: Add rollback to deploy.py**

Append to `launchbox/deploy.py`:

```python
def rollback(
    app_name: str,
    commit_sha: Optional[str] = None,
    store: Optional[StateStore] = None,
) -> str:
    """Redeploy a previous successful version without rebuilding it.

    Args:
        app_name: the application to roll back.
        commit_sha: an explicit target. Defaults to the most recent successful
            deployment that is not the one currently running.

    The promotion path is identical to a forward deployment, so a rollback that
    fails its own health probe leaves the current version serving.
    """
    owns_store = store is None
    store = store or StateStore()

    try:
        if commit_sha:
            target = store.find_successful_deployment_by_sha(app_name, commit_sha)
            if target is None:
                raise DeploymentError(
                    f"{app_name}: no successful deployment matching {commit_sha}"
                )
        else:
            target = store.last_successful_deployment(app_name)
            if target is None:
                raise DeploymentError(
                    f"{app_name}: no previous successful deployment to roll back to"
                )

        image_ref = target["image_tag"]
        if not image_ref or not runner.image_exists(image_ref):
            raise DeploymentError(
                f"{app_name}: image {image_ref} is no longer present; "
                "it cannot be rolled back to without rebuilding"
            )

        app_path = os.path.join(APPS_DIR, app_name)
        config = LaunchboxConfig(app_path)
        env_vars = _collect_environment(app_name, app_path, config)

        previous = _find_previous_container(app_name, store)
        target_sha = target["commit_sha"]
        container_name = f"{app_name}-{_short(target_sha)}"
        if runner.container_exists(container_name) or container_name == previous:
            container_name = f"{container_name}-{int(time.time())}"

        deployment_id = store.record_start(app_name, target_sha)
        logger.info(
            f"Rolling {app_name} back to {_short(target_sha)} "
            f"(deployment {deployment_id})"
        )

        return _promote(
            app_name=app_name,
            container_name=container_name,
            image_ref=image_ref,
            config=config,
            env_vars=env_vars,
            commit_sha=target_sha,
            previous_container=previous,
            store=store,
            deployment_id=deployment_id,
        )

    finally:
        if owns_store:
            store.close()
```

- [ ] **Step 4: Create the CLI**

Create `launchbox/__main__.py`:

```python
"""Unified command line entry point.

    python3 -m launchbox <command> [options]

Every module was previously invoked separately (``python3 -m launchbox.builder``
and so on), which meant deployment ordering lived in a bash hook. One entry
point keeps that ordering in one place.
"""

import argparse
import sys
from typing import List, Optional

from launchbox import runner
from launchbox.builder import build
from launchbox.deploy import deploy, rollback
from launchbox.init import init
from launchbox.logger import setup_logger, LaunchboxError
from launchbox.state import StateStore

logger = setup_logger("cli")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="launchbox",
        description="Self-hosted, git-push-to-deploy platform.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_init = sub.add_parser("init", help="register an application")
    p_init.add_argument("app_name")
    p_init.add_argument("--branch", default="main",
                        help="branch that triggers deployment (default: main)")

    p_build = sub.add_parser("build", help="build an image without deploying")
    p_build.add_argument("app_name")
    p_build.add_argument("--source", default=None)
    p_build.add_argument("--tag", default=None)

    p_deploy = sub.add_parser("deploy", help="build and deploy an application")
    p_deploy.add_argument("app_name")
    p_deploy.add_argument("--source", default=None,
                          help="directory to build (default: apps/<app>)")
    p_deploy.add_argument("--commit", default=None,
                          help="commit SHA being deployed")

    p_rollback = sub.add_parser("rollback", help="restore a previous version")
    p_rollback.add_argument("app_name")
    p_rollback.add_argument("--commit", default=None,
                            help="target commit (default: last good version)")

    p_history = sub.add_parser("history", help="show deployment history")
    p_history.add_argument("app_name")
    p_history.add_argument("--limit", type=int, default=20)

    sub.add_parser("list", help="list known applications")

    p_logs = sub.add_parser("logs", help="show container logs")
    p_logs.add_argument("app_name")
    p_logs.add_argument("--tail", type=int, default=200)

    sub.add_parser("dashboard", help="start the web dashboard")

    p_sup = sub.add_parser("supervisor", help="start the health supervisor")
    p_sup.add_argument("--interval", type=float, default=None)

    return parser


def _cmd_history(app_name: str, limit: int) -> int:
    with StateStore() as store:
        rows = store.list_deployments(app_name, limit=limit)
    if not rows:
        print(f"No deployments recorded for {app_name}")
        return 0

    print(f"{'ID':>4}  {'COMMIT':<10} {'STATUS':<12} {'STARTED':<22} ERROR")
    for row in rows:
        print(
            f"{row['id']:>4}  {(row['commit_sha'] or '-')[:9]:<10} "
            f"{row['status']:<12} {row['started_at']:<22} {row['error'] or ''}"
        )
    return 0


def _cmd_list() -> int:
    with StateStore() as store:
        apps = store.list_apps()
    if not apps:
        print("No applications deployed yet")
        return 0

    print(f"{'APP':<20} {'CONTAINER':<28} {'HEALTH':<10} RESTARTS")
    for app in apps:
        print(
            f"{app['app_name']:<20} {(app['current_container'] or '-'):<28} "
            f"{(app['health_state'] or 'unknown'):<10} {app['restart_attempts']}"
        )
    return 0


def _cmd_logs(app_name: str, tail: int) -> int:
    with StateStore() as store:
        container = store.current_container(app_name)
    if not container:
        if runner.container_exists(app_name):
            container = app_name
        else:
            print(f"No container recorded for {app_name}", file=sys.stderr)
            return 1
    print(runner.container_logs(container, tail=tail))
    return 0


def main(argv: Optional[List[str]] = None) -> int:
    args = build_parser().parse_args(argv)

    try:
        if args.command == "init":
            init(args.app_name, default_branch=args.branch)
            return 0

        if args.command == "build":
            print(build(args.app_name, source_dir=args.source, tag=args.tag))
            return 0

        if args.command == "deploy":
            print(deploy(args.app_name, source_dir=args.source,
                         commit_sha=args.commit))
            return 0

        if args.command == "rollback":
            print(rollback(args.app_name, commit_sha=args.commit))
            return 0

        if args.command == "history":
            return _cmd_history(args.app_name, args.limit)

        if args.command == "list":
            return _cmd_list()

        if args.command == "logs":
            return _cmd_logs(args.app_name, args.tail)

        if args.command == "dashboard":
            from launchbox.dashboard import main as dashboard_main

            dashboard_main()
            return 0

        if args.command == "supervisor":
            from launchbox.supervisor import run_forever

            run_forever(interval=args.interval)
            return 0

    except LaunchboxError as exc:
        logger.error(str(exc))
        return 1

    return 1


if __name__ == "__main__":
    sys.exit(main())
```

The `dashboard` and `supervisor` imports are deferred into their branches
because `dashboard.main` does not exist until Plan 2 and `supervisor` does not
exist until Task 9. Importing them at module load would break the CLI.

- [ ] **Step 5: Add the dashboard entry point it expects**

Append to `launchbox/dashboard.py`, replacing the existing `__main__` block:

```python
def main():
    """Entry point used by ``python3 -m launchbox dashboard``."""
    app.run(host="0.0.0.0", port=8000, debug=True)


if __name__ == '__main__':
    main()
```

Hardening of this entry point (auth, `debug=False`, loopback bind, port 8100)
belongs to Plan 2. This step only gives the CLI something to call.

- [ ] **Step 6: Run the tests to verify they pass**

Run: `.venv/bin/pytest tests/test_rollback.py -v`
Expected: all tests PASS. `test_cli_exposes_the_expected_commands` only builds
the argument parser and never imports `launchbox.supervisor`, so it passes even
though that module does not exist until Task 9.

- [ ] **Step 7: Verify the CLI runs**

Run: `.venv/bin/python -m launchbox --help`
Expected: usage text listing all nine commands.

Run: `.venv/bin/python -m launchbox list`
Expected: `No applications deployed yet`.

- [ ] **Step 8: Commit**

```bash
git add launchbox/deploy.py launchbox/__main__.py launchbox/dashboard.py \
        tests/test_rollback.py
git commit -m "feat: add rollback and a unified CLI

Rollback redeploys a previously recorded successful image through the same
health-gated promotion path, so a rollback that fails its own probe leaves
the current version serving. Adds python3 -m launchbox with init, build,
deploy, rollback, history, list, logs, dashboard and supervisor.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```

---

### Task 9: Health supervisor

**Files:**
- Create: `launchbox/supervisor.py`
- Test: `tests/test_supervisor.py`

**Interfaces:**
- Consumes: `StateStore`, `runner.docker_health_status`, `runner.restart_container`.
- Produces:

```python
POLL_INTERVAL: float = 30.0
MAX_RESTART_ATTEMPTS: int = 3

supervise_once(store: StateStore,
               health_fn=runner.docker_health_status,
               restart_fn=runner.restart_container,
               max_attempts: int = MAX_RESTART_ATTEMPTS) -> list[dict]
    # each action: {"app": str, "action": str, "container": str | None}
    # action is one of: healthy, restarted, marked_failed, skipped, missing

run_forever(interval: float | None = None) -> None
start_background_thread(interval: float | None = None) -> threading.Thread
```

Dependency injection on `health_fn`/`restart_fn` keeps the decision logic
testable with no Docker daemon.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_supervisor.py`:

```python
import pytest

from launchbox import supervisor
from launchbox.state import StateStore


@pytest.fixture
def store(tmp_state_db):
    s = StateStore(tmp_state_db)
    yield s
    s.close()


def _deploy(store, app="myapp", sha="abc1234"):
    dep = store.record_start(app, sha)
    store.record_success(dep, f"{app}-{sha}", f"launchbox-{app}:{sha}")
    return f"{app}-{sha}"


def test_healthy_container_resets_restart_attempts(store):
    _deploy(store)
    store.increment_restart_attempts("myapp")
    store.increment_restart_attempts("myapp")

    actions = supervisor.supervise_once(
        store, health_fn=lambda _c: "healthy", restart_fn=lambda _c: True
    )

    assert actions == [
        {"app": "myapp", "action": "healthy", "container": "myapp-abc1234"}
    ]
    assert store.restart_attempts("myapp") == 0
    assert store.get_app("myapp")["health_state"] == "healthy"


def test_unhealthy_container_is_restarted_within_the_bound(store):
    _deploy(store)
    restarted = []

    actions = supervisor.supervise_once(
        store,
        health_fn=lambda _c: "unhealthy",
        restart_fn=lambda c: restarted.append(c) or True,
        max_attempts=3,
    )

    assert restarted == ["myapp-abc1234"]
    assert actions[0]["action"] == "restarted"
    assert store.restart_attempts("myapp") == 1
    assert store.is_marked_failed("myapp") is False


def test_repeated_failures_eventually_mark_the_app_failed(store):
    _deploy(store)
    restarted = []

    for _ in range(4):
        supervisor.supervise_once(
            store,
            health_fn=lambda _c: "unhealthy",
            restart_fn=lambda c: restarted.append(c) or True,
            max_attempts=3,
        )

    assert len(restarted) == 3, "must stop restarting once the bound is reached"
    assert store.is_marked_failed("myapp") is True
    assert store.get_app("myapp")["health_state"] == "failed"


def test_a_failed_app_is_skipped_on_later_passes(store):
    _deploy(store)
    store.mark_failed("myapp")
    restarted = []

    actions = supervisor.supervise_once(
        store,
        health_fn=lambda _c: "unhealthy",
        restart_fn=lambda c: restarted.append(c) or True,
    )

    assert restarted == []
    assert actions[0]["action"] == "skipped"


def test_starting_container_is_left_alone(store):
    _deploy(store)
    restarted = []

    supervisor.supervise_once(
        store,
        health_fn=lambda _c: "starting",
        restart_fn=lambda c: restarted.append(c) or True,
    )

    assert restarted == []
    assert store.restart_attempts("myapp") == 0


def test_container_without_a_probe_is_not_restarted(store):
    """An app that declares no health_check must not be churned."""
    _deploy(store)
    restarted = []

    actions = supervisor.supervise_once(
        store,
        health_fn=lambda _c: "none",
        restart_fn=lambda c: restarted.append(c) or True,
    )

    assert restarted == []
    assert actions[0]["action"] == "healthy"


def test_missing_container_is_reported_not_restarted(store):
    _deploy(store)
    restarted = []

    actions = supervisor.supervise_once(
        store,
        health_fn=lambda _c: "missing",
        restart_fn=lambda c: restarted.append(c) or True,
    )

    assert restarted == []
    assert actions[0]["action"] == "missing"
    assert store.get_app("myapp")["health_state"] == "unknown"


def test_apps_with_no_current_container_are_ignored(store):
    store.record_start("neverdeployed", "abc1234")  # no success recorded

    actions = supervisor.supervise_once(
        store, health_fn=lambda _c: "healthy", restart_fn=lambda _c: True
    )

    assert actions == []


def test_multiple_apps_are_each_evaluated(store):
    _deploy(store, app="alpha", sha="1111111")
    _deploy(store, app="beta", sha="2222222")

    statuses = {"alpha-1111111": "healthy", "beta-2222222": "unhealthy"}
    actions = supervisor.supervise_once(
        store, health_fn=lambda c: statuses[c], restart_fn=lambda _c: True
    )

    by_app = {a["app"]: a["action"] for a in actions}
    assert by_app == {"alpha": "healthy", "beta": "restarted"}


def test_a_failed_restart_still_counts_as_an_attempt(store):
    _deploy(store)

    actions = supervisor.supervise_once(
        store, health_fn=lambda _c: "unhealthy", restart_fn=lambda _c: False
    )

    assert actions[0]["action"] == "restart_failed"
    assert store.restart_attempts("myapp") == 1


def test_start_background_thread_is_a_daemon(mocker):
    mocker.patch.object(supervisor, "run_forever")

    thread = supervisor.start_background_thread(interval=1)

    assert thread.daemon is True, "must not block interpreter shutdown"
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `.venv/bin/pytest tests/test_supervisor.py -v`
Expected: `ModuleNotFoundError: No module named 'launchbox.supervisor'`.

- [ ] **Step 3: Write the implementation**

Create `launchbox/supervisor.py`:

```python
"""Health supervision.

Docker reports whether a container is healthy; on its own, nothing acts on that
report. This module closes that loop: an unhealthy container is restarted, and
one that keeps failing is marked failed rather than restarted forever, so a
genuine crash loop cannot consume the host.
"""

import os
import threading
import time
from typing import Any, Callable, Dict, List, Optional

from launchbox import runner
from launchbox.logger import setup_logger
from launchbox.state import StateStore

logger = setup_logger("supervisor")

POLL_INTERVAL = float(os.environ.get("LAUNCHBOX_SUPERVISOR_INTERVAL", 30))
MAX_RESTART_ATTEMPTS = int(os.environ.get("LAUNCHBOX_MAX_RESTARTS", 3))


def supervise_once(
    store: StateStore,
    health_fn: Callable[[str], str] = runner.docker_health_status,
    restart_fn: Callable[[str], bool] = runner.restart_container,
    max_attempts: int = MAX_RESTART_ATTEMPTS,
) -> List[Dict[str, Any]]:
    """Evaluate every deployed application once.

    Returns the actions taken, which makes the decision logic testable without
    a Docker daemon and gives the dashboard something to display.
    """
    actions: List[Dict[str, Any]] = []

    for app in store.list_apps():
        app_name = app["app_name"]
        container = app["current_container"]

        if not container:
            continue

        if store.is_marked_failed(app_name):
            actions.append(
                {"app": app_name, "action": "skipped", "container": container}
            )
            continue

        status = health_fn(container)

        if status in ("healthy", "none"):
            # "none" means the container declares no probe. Absence of a probe
            # is not evidence of failure, so leave it alone.
            store.set_health(app_name, "healthy")
            store.reset_restart_attempts(app_name)
            actions.append(
                {"app": app_name, "action": "healthy", "container": container}
            )
            continue

        if status == "starting":
            store.set_health(app_name, "starting")
            actions.append(
                {"app": app_name, "action": "starting", "container": container}
            )
            continue

        if status == "missing":
            store.set_health(app_name, "unknown")
            logger.warning(f"{app_name}: container {container} is gone")
            actions.append(
                {"app": app_name, "action": "missing", "container": container}
            )
            continue

        # status == "unhealthy"
        store.set_health(app_name, "unhealthy")
        attempts = store.restart_attempts(app_name)

        if attempts >= max_attempts:
            store.mark_failed(app_name)
            logger.error(
                f"{app_name}: still unhealthy after {attempts} restarts; "
                "marking failed and giving up"
            )
            actions.append(
                {"app": app_name, "action": "marked_failed", "container": container}
            )
            continue

        logger.warning(
            f"{app_name}: unhealthy, restarting "
            f"(attempt {attempts + 1} of {max_attempts})"
        )
        ok = restart_fn(container)
        store.increment_restart_attempts(app_name)
        actions.append({
            "app": app_name,
            "action": "restarted" if ok else "restart_failed",
            "container": container,
        })

    return actions


def run_forever(interval: Optional[float] = None) -> None:
    """Poll indefinitely. Used by ``python3 -m launchbox supervisor``."""
    poll = interval or POLL_INTERVAL
    logger.info(f"Supervisor started, polling every {poll:.0f}s")

    store = StateStore()
    try:
        while True:
            try:
                supervise_once(store)
            except Exception as exc:  # never let one bad pass kill the loop
                logger.error(f"Supervisor pass failed: {exc}")
            time.sleep(poll)
    finally:
        store.close()


def start_background_thread(interval: Optional[float] = None) -> threading.Thread:
    """Run the supervisor alongside another process, such as the dashboard."""
    thread = threading.Thread(
        target=run_forever,
        kwargs={"interval": interval},
        name="launchbox-supervisor",
        daemon=True,
    )
    thread.start()
    return thread
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `.venv/bin/pytest tests/test_supervisor.py -v`
Expected: all tests PASS.

- [ ] **Step 5: Run the whole suite**

Run: `.venv/bin/pytest -v`
Expected: all tests PASS.

- [ ] **Step 6: Verify the supervisor starts and stops cleanly**

Run: `timeout 3 .venv/bin/python -m launchbox supervisor --interval 1; echo "exit=$?"`
Expected: log lines showing the supervisor started; `exit=124` (timeout killed it), which is correct for a daemon.

- [ ] **Step 7: Commit**

```bash
git add launchbox/supervisor.py tests/test_supervisor.py
git commit -m "feat: add bounded restart supervision

Polls Docker-reported container health and restarts an unhealthy container
up to a configurable bound, then marks the application failed rather than
restarting it indefinitely. Runs standalone or as a daemon thread beside
the dashboard.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```

---

## Verification

After Task 9, run the full suite and confirm the headline guarantee is covered:

```bash
.venv/bin/pytest -v
.venv/bin/pytest -k "leaves_the_old_container_running or never_touches" -v
```

Expected: all pass, including
`test_failed_health_probe_leaves_the_old_container_running` and
`test_failed_build_never_touches_the_running_container`. Those two tests are the
proof that Chapter 4.12 gaps 1 and 2 are closed.

## What Plan 2 covers

Not started here, and dependent on this plan landing first:

1. Per-application generated database credentials, named data volumes, the
   `mongosh` readiness fix, and removal of `POSTGRES_HOST_AUTH_METHOD=trust`.
2. Dashboard hardening (auth, `debug=False`, loopback bind, port 8100) plus the
   history, rollback and health UI, and the missing `app_detail.html`.
3. `apps/mysql_app` and `apps/mongo_app`, and HTTPS enabled on `test_app`, with
   mkcert installed by `setup.sh`.
4. Docker-marked integration tests exercising a real build, swap, failed build,
   failed probe and rollback.
5. `IMPROVEMENTS.md`, mapping each row of Table 5.2 of the dissertation onto what
   was implemented and how it was verified.
