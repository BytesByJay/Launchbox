"""Tests for database provisioning being recorded into the state store.

create_database_for_app() previously had zero production callers of
StateStore.record_database() anywhere in the codebase: the dashboard's
database panel and `launchbox list` could never show a database that had
genuinely been provisioned. This module tests the fix, mocking the Docker
client so no daemon is required.
"""

import docker
import pytest

from launchbox.config_parser import LaunchboxConfig
from launchbox.database_manager import DatabaseManager
from launchbox.state import StateStore


@pytest.fixture
def store(tmp_state_db):
    s = StateStore(tmp_state_db)
    yield s
    s.close()


@pytest.fixture
def manager(mocker, store):
    """A DatabaseManager with no real Docker connection, pointed at the test
    state database instead of the real one.
    """
    from launchbox import database_manager as database_manager_module

    mocker.patch.object(database_manager_module, "StateStore",
                        lambda *a, **k: StateStore(store.db_path))

    m = DatabaseManager.__new__(DatabaseManager)
    m.client = mocker.MagicMock()
    return m


def _enabled_config(engine="postgresql", version="13", name="myapp_db"):
    config = LaunchboxConfig.from_mapping({
        "database": {"enabled": True, "type": engine, "version": version,
                    "name": name},
    })
    return config


class _FakeContainer:
    def __init__(self, status="running"):
        self.status = status
        self.started = False

    def start(self):
        self.started = True
        self.status = "running"

    def exec_run(self, *a, **k):
        return type("Result", (), {"exit_code": 0})()


# --------------------------------------------------------------- new container


def test_provisioning_a_new_postgres_database_records_state(manager, store,
                                                              mocker):
    manager.client.containers.get.side_effect = docker.errors.NotFound("none")
    manager.client.containers.run.return_value = _FakeContainer()
    mocker.patch.object(manager, "_wait_for_postgres")

    manager.create_database_for_app("myapp", "/unused",
                                    config=_enabled_config("postgresql"))

    row = store.get_database("myapp")
    assert row is not None
    assert row["engine"] == "postgresql"
    assert row["container_name"] == "myapp_postgres"
    assert row["db_name"] == "myapp_db"
    assert row["username"] == "launchbox"
    assert row["password"], "a password must be recorded"
    assert row["password"] != "launchbox123", (
        "a new database must not use the historical shared password"
    )


def test_recorded_volume_name_is_honestly_empty_not_a_fake_volume(
    manager, store, mocker
):
    """No engine mounts a named volume. Recording a plausible-looking volume
    name here would claim persistence that does not exist.
    """
    manager.client.containers.get.side_effect = docker.errors.NotFound("none")
    manager.client.containers.run.return_value = _FakeContainer()
    mocker.patch.object(manager, "_wait_for_postgres")

    manager.create_database_for_app("myapp", "/unused",
                                    config=_enabled_config("postgresql"))

    assert store.get_database("myapp")["volume_name"] == ""


@pytest.mark.parametrize("engine,container_suffix", [
    ("postgresql", "postgres"), ("mysql", "mysql"), ("mongodb", "mongodb"),
])
def test_provisioning_records_the_correct_engine_and_host(
    manager, store, mocker, engine, container_suffix
):
    manager.client.containers.get.side_effect = docker.errors.NotFound("none")
    manager.client.containers.run.return_value = _FakeContainer()
    mocker.patch.object(manager, f"_wait_for_{container_suffix}")

    manager.create_database_for_app("myapp", "/unused",
                                    config=_enabled_config(engine))

    row = store.get_database("myapp")
    assert row["engine"] == engine
    assert row["container_name"] == f"myapp_{container_suffix}"


# ------------------------------------------------------------ existing container


def test_reusing_a_running_container_still_records_state(manager, store):
    manager.client.containers.get.return_value = _FakeContainer(status="running")

    manager.create_database_for_app("myapp", "/unused",
                                    config=_enabled_config("postgresql"))

    assert store.get_database("myapp") is not None
    manager.client.containers.run.assert_not_called()


def test_starting_a_stopped_container_still_records_state(manager, store):
    stopped = _FakeContainer(status="exited")
    manager.client.containers.get.return_value = stopped

    manager.create_database_for_app("myapp", "/unused",
                                    config=_enabled_config("postgresql"))

    assert stopped.started is True
    assert store.get_database("myapp") is not None


# ---------------------------------------------------------------------- gating


def test_disabled_database_provisions_nothing_and_records_nothing(manager,
                                                                   store):
    config = LaunchboxConfig.from_mapping({"database": {"enabled": False}})

    result = manager.create_database_for_app("myapp", "/unused", config=config)

    assert result is None
    assert store.get_database("myapp") is None
    manager.client.containers.get.assert_not_called()


# -------------------------------------------------------- state store tolerance


def test_provisioning_succeeds_even_if_the_state_store_is_unreachable(
    mocker, store
):
    """Bookkeeping must never be allowed to break provisioning itself."""
    from launchbox import database_manager as database_manager_module

    mocker.patch.object(database_manager_module, "StateStore",
                        side_effect=RuntimeError("database is locked"))

    m = DatabaseManager.__new__(DatabaseManager)
    m.client = mocker.MagicMock()
    m.client.containers.get.side_effect = docker.errors.NotFound("none")
    m.client.containers.run.return_value = _FakeContainer()
    mocker.patch.object(m, "_wait_for_postgres")

    result = m.create_database_for_app("myapp", "/unused",
                                       config=_enabled_config("postgresql"))

    assert result is not None
    assert result["DB_HOST"] == "myapp_postgres"


# ===================================================================
# Per-application credentials
# ===================================================================


def _provision(manager, mocker, app_name="myapp", engine="postgresql",
               waiter="_wait_for_postgres"):
    manager.client.containers.get.side_effect = docker.errors.NotFound("none")
    manager.client.containers.run.return_value = _FakeContainer()
    mocker.patch.object(manager, waiter)
    return manager.create_database_for_app(
        app_name, "/unused", config=_enabled_config(engine)
    )


def test_a_new_database_gets_a_generated_password(manager, store, mocker):
    info = _provision(manager, mocker)

    assert info["DB_PASSWORD"] != "launchbox123"
    assert len(info["DB_PASSWORD"]) >= 20, "must not be trivially guessable"


def test_the_generated_password_reaches_the_container_environment(
    manager, store, mocker
):
    """The password must be what Postgres is actually initialised with, not
    just what is handed to the application -- otherwise the two disagree.
    """
    info = _provision(manager, mocker)

    env = manager.client.containers.run.call_args.kwargs["environment"]
    assert env["POSTGRES_PASSWORD"] == info["DB_PASSWORD"]
    assert env["POSTGRES_USER"] == info["DB_USER"]


def test_trust_auth_is_not_enabled(manager, store, mocker):
    """POSTGRES_HOST_AUTH_METHOD=trust accepts any connection without
    checking a password, which would make the generated password
    decorative.
    """
    _provision(manager, mocker)

    env = manager.client.containers.run.call_args.kwargs["environment"]
    assert "POSTGRES_HOST_AUTH_METHOD" not in env


def test_two_applications_get_different_passwords(manager, store, mocker):
    first = _provision(manager, mocker, app_name="alpha")
    second = _provision(manager, mocker, app_name="beta")

    assert first["DB_PASSWORD"] != second["DB_PASSWORD"]


def test_the_password_is_safe_to_embed_in_a_connection_url(manager, store,
                                                            mocker):
    """A password containing ':', '@' or '/' would silently corrupt
    DATABASE_URL for some passwords and not others.
    """
    from urllib.parse import urlparse

    for _ in range(25):
        manager.client.reset_mock()
        info = _provision(manager, mocker)
        password = info["DB_PASSWORD"]

        assert not (set(password) & set(':@/?#[]')), (
            f"password contains a URI-reserved character: {password!r}"
        )
        parsed = urlparse(info["DATABASE_URL"])
        assert parsed.password == password, (
            "the password must survive a round trip through the URL"
        )
        assert parsed.hostname == "myapp_postgres"


# ------------------------------------------------------------- idempotency


def test_reprovisioning_reuses_the_stored_password(manager, store, mocker):
    """Provisioning runs on every deployment. Generating a new password on
    the second run would hand the application one its database rejects.
    """
    first = _provision(manager, mocker)

    manager.client.containers.get.side_effect = None
    manager.client.containers.get.return_value = _FakeContainer("running")
    second = manager.create_database_for_app(
        "myapp", "/unused", config=_enabled_config("postgresql")
    )

    assert second["DB_PASSWORD"] == first["DB_PASSWORD"]


def test_switching_engine_does_not_reuse_the_other_engines_password(
    manager, store, mocker
):
    """A different engine means a different container, whose password was
    never the one recorded for the old one.
    """
    postgres = _provision(manager, mocker, engine="postgresql")
    mysql = _provision(manager, mocker, engine="mysql",
                       waiter="_wait_for_mysql")

    assert mysql["DB_PASSWORD"] != postgres["DB_PASSWORD"]
    assert store.get_database("myapp")["engine"] == "mysql"


# --------------------------------------------------------- legacy adoption


def test_an_existing_container_with_no_record_keeps_the_legacy_password(
    manager, store
):
    """A database provisioned before this change has its password baked into
    its data directory. Generating a new one would lock the application out
    of its own database.
    """
    manager.client.containers.get.return_value = _FakeContainer("running")

    info = manager.create_database_for_app(
        "legacyapp", "/unused", config=_enabled_config("postgresql")
    )

    assert info["DB_PASSWORD"] == "launchbox123"
    assert info["DB_USER"] == "launchbox"


def test_legacy_adoption_is_recorded_so_it_only_happens_once(manager, store):
    manager.client.containers.get.return_value = _FakeContainer("running")

    manager.create_database_for_app("legacyapp", "/unused",
                                    config=_enabled_config("postgresql"))

    assert store.get_database("legacyapp")["password"] == "launchbox123"


def test_a_stopped_legacy_container_is_also_adopted_not_regenerated(
    manager, store
):
    manager.client.containers.get.return_value = _FakeContainer("exited")

    info = manager.create_database_for_app(
        "legacyapp", "/unused", config=_enabled_config("postgresql")
    )

    assert info["DB_PASSWORD"] == "launchbox123"


def test_an_unreachable_daemon_does_not_cause_a_password_to_be_generated(
    manager, store
):
    """Treating a daemon error as 'container absent' would generate a
    password for a container that already exists with a different one.
    """
    from launchbox.database_manager import DatabaseError

    manager.client.containers.get.side_effect = RuntimeError("daemon gone")

    with pytest.raises(DatabaseError):
        manager.create_database_for_app("myapp", "/unused",
                                        config=_enabled_config("postgresql"))


# ----------------------------------------------------------------- probes


def test_the_mysql_readiness_probe_uses_the_generated_password(manager, store,
                                                                mocker):
    """The probe hardcoded -plaunchbox123, so against a container with a
    generated password it could only ever time out.
    """
    manager.client.containers.get.side_effect = docker.errors.NotFound("none")
    manager.client.containers.run.return_value = _FakeContainer()
    probe = mocker.patch.object(manager, "_wait_for_mysql")

    info = manager.create_database_for_app("myapp", "/unused",
                                           config=_enabled_config("mysql"))

    assert probe.call_args.kwargs["password"] == info["DB_PASSWORD"]
    assert probe.call_args.kwargs["username"] == info["DB_USER"]


def test_the_mysql_root_password_is_not_a_known_constant(manager, store,
                                                          mocker):
    manager.client.containers.get.side_effect = docker.errors.NotFound("none")
    manager.client.containers.run.return_value = _FakeContainer()
    mocker.patch.object(manager, "_wait_for_mysql")

    manager.create_database_for_app("myapp", "/unused",
                                    config=_enabled_config("mysql"))

    env = manager.client.containers.run.call_args.kwargs["environment"]
    assert env["MYSQL_ROOT_PASSWORD"] != "rootpass123"
    assert env["MYSQL_ROOT_PASSWORD"] != env["MYSQL_PASSWORD"]
