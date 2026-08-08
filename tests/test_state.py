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


# --------------------------------------------------- deployment config capture


PRE_MIGRATION_SCHEMA = """
CREATE TABLE deployments (
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
CREATE TABLE apps (
    app_name           TEXT PRIMARY KEY,
    current_container  TEXT,
    current_image      TEXT,
    current_deployment INTEGER,
    health_state       TEXT,
    restart_attempts   INTEGER NOT NULL DEFAULT 0,
    marked_failed      INTEGER NOT NULL DEFAULT 0
);
"""


def test_opening_a_pre_migration_database_adds_columns_and_keeps_rows(
    tmp_state_db,
):
    """An existing state/launchbox.db has the deployments table but not the
    columns added for config capture. CREATE TABLE IF NOT EXISTS silently does
    nothing for an existing table, so without an explicit migration every read
    of config_json would fail with 'no such column' on a real installation.
    """
    import sqlite3

    conn = sqlite3.connect(tmp_state_db)
    conn.executescript(PRE_MIGRATION_SCHEMA)
    conn.execute(
        "INSERT INTO deployments (app_name, commit_sha, image_tag, "
        "container_name, status, started_at) VALUES (?, ?, ?, ?, ?, ?)",
        ("legacyapp", "abc1234", "launchbox-legacyapp:abc1234",
         "legacyapp", "success", "2026-01-01T00:00:00+00:00"),
    )
    conn.commit()
    conn.close()

    store = StateStore(tmp_state_db)
    try:
        rows = store.list_deployments("legacyapp")
        assert len(rows) == 1, "the pre-existing row must survive the migration"
        assert rows[0]["commit_sha"] == "abc1234"
        assert rows[0]["container_name"] == "legacyapp"
        # The new columns exist and read as NULL for the old row.
        assert rows[0]["config_json"] is None
        assert rows[0]["env_json"] is None
        assert rows[0]["image_id"] is None
    finally:
        store.close()


def test_migration_is_idempotent_across_reopens(tmp_state_db):
    first = StateStore(tmp_state_db)
    dep = first.record_start("myapp", "abc1234", config_json='{"a": 1}')
    first.close()

    second = StateStore(tmp_state_db)
    try:
        assert second.get_deployment(dep)["config_json"] == '{"a": 1}'
    finally:
        second.close()


def test_record_start_still_accepts_two_arguments(store):
    """Other callers and the existing tests use the two-argument form."""
    dep = store.record_start("myapp", "abc1234")
    row = store.get_deployment(dep)
    assert row["config_json"] is None
    assert row["env_json"] is None


def test_record_start_persists_config_and_environment(store):
    dep = store.record_start(
        "myapp", "abc1234",
        config_json='{"app": {"port": 8080}}',
        env_json='{"NODE_ENV": "production"}',
    )
    row = store.get_deployment(dep)
    assert row["config_json"] == '{"app": {"port": 8080}}'
    assert row["env_json"] == '{"NODE_ENV": "production"}'


def test_record_config_attaches_to_an_open_row(store):
    dep = store.record_start("myapp", "abc1234")
    store.record_config(dep, '{"app": {"port": 8080}}', '{"A": "b"}')

    row = store.get_deployment(dep)
    assert row["config_json"] == '{"app": {"port": 8080}}'
    assert row["env_json"] == '{"A": "b"}'
    assert row["status"] == "in_progress", "recording config must not close the row"


def test_record_success_stores_the_immutable_image_id(store):
    dep = store.record_start("myapp", "abc1234")
    store.record_success(
        dep, "myapp-abc1234", "launchbox-myapp:latest",
        image_id="sha256:deadbeef",
    )
    assert store.get_deployment(dep)["image_id"] == "sha256:deadbeef"


def test_record_success_image_id_is_optional(store):
    dep = store.record_start("myapp", "abc1234")
    store.record_success(dep, "myapp-abc1234", "launchbox-myapp:abc1234")
    assert store.get_deployment(dep)["image_id"] is None


# ------------------------------------------------------ LIKE wildcard escaping


def test_find_by_sha_does_not_treat_underscore_as_a_wildcard(store):
    """`rollback --commit` passes a raw command-line string straight into a
    LIKE pattern. Unescaped, `_` matches any single character, so
    `--commit _` matched an arbitrary deployment and rolled onto it.
    """
    dep = store.record_start("myapp", "abc1234")
    store.record_success(dep, "myapp-abc1234", "launchbox-myapp:abc1234")

    assert store.find_successful_deployment_by_sha("myapp", "_") is None
    assert store.find_successful_deployment_by_sha("myapp", "_bc1234") is None


def test_find_by_sha_does_not_treat_percent_as_a_wildcard(store):
    dep = store.record_start("myapp", "abc1234")
    store.record_success(dep, "myapp-abc1234", "launchbox-myapp:abc1234")

    assert store.find_successful_deployment_by_sha("myapp", "%") is None
    assert store.find_successful_deployment_by_sha("myapp", "a%") is None


def test_find_by_sha_still_matches_a_genuine_prefix(store):
    dep = store.record_start("myapp", "abc1234deadbeef")
    store.record_success(dep, "myapp-abc1234", "launchbox-myapp:abc1234")

    assert store.find_successful_deployment_by_sha("myapp", "abc")["id"] == dep
    assert store.find_successful_deployment_by_sha(
        "myapp", "abc1234deadbeef"
    )["id"] == dep


def test_find_by_sha_matches_a_literal_underscore_when_present(store):
    """Escaping must make `_` literal, not unmatchable."""
    dep = store.record_start("myapp", "a_b1234")
    store.record_success(dep, "myapp-a_b1234", "launchbox-myapp:a_b1234")

    assert store.find_successful_deployment_by_sha("myapp", "a_b")["id"] == dep
    assert store.find_successful_deployment_by_sha("myapp", "axb") is None


# --------------------------------------------------------- register / activity


def test_register_makes_an_app_visible_before_any_deployment(store):
    assert store.get_app("freshapp") is None

    store.register("freshapp")

    app = store.get_app("freshapp")
    assert app is not None
    assert app["current_container"] is None
    assert app["current_image"] is None


def test_register_is_idempotent(store):
    store.register("freshapp")
    store.register("freshapp")

    assert len(store.list_apps()) == 1


def test_register_does_not_disturb_an_already_deployed_app(store):
    dep = store.record_start("myapp", "abc1234")
    store.record_success(dep, "myapp-abc1234", "launchbox-myapp:abc1234")

    store.register("myapp")

    assert store.current_container("myapp") == "myapp-abc1234"


def test_list_recent_deployments_spans_every_app_newest_first(store):
    for app_name, sha in [("alpha", "1111111"), ("beta", "2222222"),
                          ("alpha", "3333333")]:
        dep = store.record_start(app_name, sha)
        store.record_success(dep, f"{app_name}-{sha}",
                             f"launchbox-{app_name}:{sha}")

    recent = store.list_recent_deployments(limit=10)

    assert [r["commit_sha"] for r in recent] == ["3333333", "2222222", "1111111"]
    assert [r["app_name"] for r in recent] == ["alpha", "beta", "alpha"]


def test_list_recent_deployments_respects_the_limit(store):
    for i in range(5):
        dep = store.record_start("myapp", f"sha{i}")
        store.record_success(dep, f"myapp-sha{i}", f"launchbox-myapp:sha{i}")

    assert len(store.list_recent_deployments(limit=2)) == 2


def test_list_recent_deployments_includes_failures(store):
    dep = store.record_start("myapp", "bad0000")
    store.record_failure(dep, "build failed")

    recent = store.list_recent_deployments()

    assert recent[0]["status"] == "failed"
    assert recent[0]["error"] == "build failed"


# ------------------------------------------------------------- deployment kind


def test_record_start_defaults_kind_to_deploy(store):
    dep_id = store.record_start("myapp", "abc1234")
    assert store.get_deployment(dep_id)["kind"] == "deploy"


def test_record_start_persists_an_explicit_kind(store):
    dep_id = store.record_start("myapp", "abc1234", kind="rollback")
    assert store.get_deployment(dep_id)["kind"] == "rollback"


def test_a_row_predating_the_kind_column_reads_as_none_not_an_error(
    tmp_path
):
    """A database written before this column existed must still open and
    read cleanly -- the reading side is what decides an absent kind means
    'deploy', not a NULL constraint at write time.
    """
    import sqlite3

    db_path = str(tmp_path / "pre-kind.db")
    conn = sqlite3.connect(db_path)
    conn.execute(
        "CREATE TABLE deployments (id INTEGER PRIMARY KEY AUTOINCREMENT, "
        "app_name TEXT NOT NULL, commit_sha TEXT, image_tag TEXT, "
        "container_name TEXT, status TEXT NOT NULL, started_at TEXT NOT NULL, "
        "finished_at TEXT, error TEXT, logs TEXT)"
    )
    conn.execute(
        "INSERT INTO deployments (app_name, commit_sha, status, started_at) "
        "VALUES ('myapp', 'abc1234', 'success', '2026-01-01T00:00:00+00:00')"
    )
    conn.commit()
    conn.close()

    store = StateStore(db_path)
    try:
        row = store.list_deployments("myapp")[0]
        assert row["kind"] is None
    finally:
        store.close()
