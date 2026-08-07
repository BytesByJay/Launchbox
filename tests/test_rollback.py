import json

import pytest

from launchbox.logger import DeploymentError
from launchbox.state import StateStore

# The configuration a deployment is recorded as having run with. Rollback
# replays this instead of re-reading apps/<app>/, which for a git-pushed
# application never exists.
RECORDED_CONFIG = {
    "app": {
        "port": 3000,
        "health_check": None,
        "build": {"dockerfile": "Dockerfile", "context": "."},
    },
    "resources": {"memory": None, "cpu": None},
    "environment": {},
    "database": {"enabled": False, "type": "postgresql", "version": "13",
                 "name": None},
    "https": {"enabled": False, "redirect_http": True},
}


def record_deployment(store, sha, config=None, env=None, image_id=None,
                      image_tag=None):
    """A successful deployment row carrying captured configuration."""
    dep = store.record_start(
        "myapp", sha,
        config_json=None if config is None else json.dumps(config),
        env_json=None if env is None else json.dumps(env),
    )
    store.record_success(
        dep, f"myapp-{sha}", image_tag or f"launchbox-myapp:{sha}",
        image_id=image_id,
    )
    return dep


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
    old = record_deployment(store, "1111111", config=RECORDED_CONFIG, env={})
    new = record_deployment(store, "2222222", config=RECORDED_CONFIG, env={})
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
        "find_containers": mocker.patch.object(
            deploy_module.runner, "find_app_containers", return_value=[]
        ),
        "image_id": mocker.patch.object(
            deploy_module.runner, "image_id", return_value=None
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
        record_deployment(store, sha, config=RECORDED_CONFIG, env={})

    rollback("myapp", commit_sha="1111111", store=store)

    assert fake_docker["create"].call_args.kwargs["image_ref"] == (
        "launchbox-myapp:1111111"
    )


def test_rollback_fails_when_there_is_nothing_to_roll_back_to(store, fake_docker):
    from launchbox.deploy import rollback

    record_deployment(store, "1111111", config=RECORDED_CONFIG, env={})

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


def test_rollback_records_failure_when_create_container_raises(
    store, two_deployments, fake_docker
):
    """A DeploymentError raised by create_container (e.g. an unreachable
    docker daemon) happens inside _promote, outside its own probe-failure
    branch. Before the fix, rollback() had no exception handling around
    _promote at all, so nothing recorded it and the row was left stuck at
    status='in_progress' forever -- silently corrupting the history even
    though the running container was correctly left untouched.
    """
    from launchbox.deploy import rollback

    fake_docker["create"].side_effect = DeploymentError("docker daemon unreachable")

    with pytest.raises(DeploymentError, match="docker daemon unreachable"):
        rollback("myapp", store=store)

    record = store.list_deployments("myapp")[0]
    assert record["status"] == "failed"
    assert record["error"]

    removed = [c[0][0] for c in fake_docker["remove"].call_args_list]
    assert "myapp-2222222" not in removed
    fake_docker["write_route"].assert_not_called()
    assert store.current_container("myapp") == "myapp-2222222"


def test_failed_rollback_probe_captures_logs_into_history(
    store, two_deployments, fake_docker
):
    """Mirror of the deploy-side test_failed_health_probe_captures_logs_into_history:
    a rollback whose own health probe fails must still end with the failed
    container's logs attached to the deployment row, and _run_promotion must
    not overwrite that richer record with a generic failure message.
    """
    from launchbox.deploy import rollback

    fake_docker["health"].return_value = False
    fake_docker["logs"].return_value = "crash trace"

    with pytest.raises(DeploymentError, match="health"):
        rollback("myapp", store=store)

    record = store.list_deployments("myapp")[0]
    assert record["status"] == "failed"
    assert record["logs"] == "crash trace"


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


@pytest.mark.parametrize("argv", [
    ["deploy", "bad name!"],
    ["init", "bad name!"],
    ["build", "bad name!"],
    ["rollback", "bad name!"],
])
def test_cli_reports_an_invalid_app_name_cleanly_instead_of_a_traceback(argv):
    """validate_app_name raises plain ValueError, not a LaunchboxError
    subclass. Before the fix, main() only caught LaunchboxError, so an
    invalid app name reached argparse successfully and then blew up with an
    uncaught ValueError -- a raw traceback on stderr, with exit code 1 only
    because that is Python's default for an unhandled exception rather than
    anything main() did on purpose. None of these commands touch Docker or
    the state store: validate_app_name is the first thing each of
    init/build/deploy/rollback does, so this exercises the real functions
    without mocking anything.
    """
    from launchbox import __main__ as cli

    assert cli.main(argv) == 1


def test_cli_valid_app_name_still_returns_zero(mocker):
    """Guard against the ValueError catch in main() being so broad it
    swallows a real success. A legitimate name must still reach the
    (mocked) command function and return 0.
    """
    from launchbox import __main__ as cli

    mocker.patch.object(cli, "deploy", return_value="myapp-abc1234")

    assert cli.main(["deploy", "myapp"]) == 0


# ----------------------------------------- rollback replays the stored config


CONFIGURED = {
    "app": {
        "port": 8080,
        "health_check": {"path": "/healthz", "interval": "5s",
                         "timeout": "2s", "retries": 4},
        "build": {"dockerfile": "Dockerfile", "context": "."},
    },
    "resources": {"memory": "512m", "cpu": 0.5},
    "environment": {"NODE_ENV": "production"},
    "database": {"enabled": False, "type": "postgresql", "version": "13",
                 "name": None},
    "https": {"enabled": True, "redirect_http": True},
}


def test_rollback_replays_the_stored_port_and_health_check(store, fake_docker):
    """The defect this closes: rollback built its config from
    LaunchboxConfig(apps/<app>), and for a git-pushed application that
    directory never exists. LaunchboxConfig does not raise on a missing
    directory -- it returns built-in defaults -- so the rolled-back container
    came up on port 3000 with no health check, no resource limits and no
    environment, and _promote then removed the healthy container and published
    a route to something not serving.
    """
    from launchbox.deploy import rollback

    record_deployment(store, "1111111", config=CONFIGURED,
                      env={"NODE_ENV": "production"})
    record_deployment(store, "2222222", config=RECORDED_CONFIG, env={})

    rollback("myapp", store=store)

    kwargs = fake_docker["create"].call_args.kwargs
    assert kwargs["port"] == 8080
    assert kwargs["health_check"] == {
        "path": "/healthz", "interval": "5s", "timeout": "2s", "retries": 4
    }
    assert kwargs["resource_limits"] == {"memory": "512m", "cpus": "0.5"}
    assert kwargs["env_vars"]["NODE_ENV"] == "production"


def test_rollback_replays_the_stored_https_settings(store, fake_docker):
    from launchbox.deploy import rollback

    record_deployment(store, "1111111", config=CONFIGURED, env={})
    record_deployment(store, "2222222", config=RECORDED_CONFIG, env={})

    rollback("myapp", store=store)

    assert fake_docker["write_route"].call_args.kwargs["https"] is True
    assert fake_docker["write_route"].call_args.args[2] == 8080


def test_rollback_refuses_a_row_that_predates_config_capture(store,
                                                             fake_docker):
    """Silently falling back to defaults IS the bug. A row with no captured
    configuration cannot be safely replayed, so it must be refused loudly
    rather than guessed at.
    """
    from launchbox.deploy import rollback

    record_deployment(store, "1111111", config=None, env=None)
    record_deployment(store, "2222222", config=RECORDED_CONFIG, env={})

    with pytest.raises(DeploymentError, match="predates"):
        rollback("myapp", store=store)

    fake_docker["create"].assert_not_called()
    fake_docker["remove"].assert_not_called()
    fake_docker["write_route"].assert_not_called()
    assert store.current_container("myapp") == "myapp-2222222"


def test_rollback_does_not_read_the_apps_directory(store, mocker,
                                                   monkeypatch, tmp_path):
    """apps/<app>/ being absent entirely -- the normal state for a git-pushed
    application -- must not affect a rollback that has stored config.
    """
    import os

    from launchbox import deploy as deploy_module
    from launchbox.deploy import rollback

    monkeypatch.setattr(deploy_module, "APPS_DIR", str(tmp_path / "nonexistent"))
    assert not os.path.exists(
        os.path.join(str(tmp_path / "nonexistent"), "myapp")
    )

    mocker.patch.object(deploy_module.runner, "image_exists", return_value=True)
    create = mocker.patch.object(deploy_module.runner, "create_container",
                                 return_value="cid")
    mocker.patch.object(deploy_module.runner, "wait_for_health",
                        return_value=True)
    mocker.patch.object(deploy_module.runner, "stop_and_remove",
                        return_value=True)
    mocker.patch.object(deploy_module.runner, "container_exists",
                        return_value=False)
    mocker.patch.object(deploy_module.runner, "find_app_containers",
                        return_value=[])
    mocker.patch.object(deploy_module.runner, "image_id", return_value=None)
    mocker.patch.object(deploy_module.router, "write_route", return_value="p")

    record_deployment(store, "1111111", config=CONFIGURED, env={})
    record_deployment(store, "2222222", config=RECORDED_CONFIG, env={})

    rollback("myapp", store=store)

    assert create.call_args.kwargs["port"] == 8080


def test_rollback_records_the_config_it_replayed(store, fake_docker):
    """So that rolling back to a rollback works too."""
    from launchbox.deploy import rollback

    record_deployment(store, "1111111", config=CONFIGURED, env={})
    record_deployment(store, "2222222", config=RECORDED_CONFIG, env={})

    rollback("myapp", store=store)

    row = store.list_deployments("myapp")[0]
    assert json.loads(row["config_json"])["app"]["port"] == 8080


# -------------------------------------- database credentials are not replayed


def test_rollback_reprovisions_the_database_instead_of_replaying_credentials(
    store, fake_docker, mocker
):
    """Provisioning is idempotent and the credentials the database container
    has now are the authoritative ones; a stale DATABASE_URL captured months
    ago is not.
    """
    from launchbox import deploy as deploy_module
    from launchbox.deploy import rollback

    db_config = json.loads(json.dumps(CONFIGURED))
    db_config["database"]["enabled"] = True

    record_deployment(
        store, "1111111", config=db_config,
        env={"NODE_ENV": "production", "DATABASE_URL": "postgresql://stale"},
    )
    record_deployment(store, "2222222", config=RECORDED_CONFIG, env={})

    manager = mocker.patch.object(deploy_module, "DatabaseManager")
    manager.return_value.create_database_for_app.return_value = {
        "DATABASE_URL": "postgresql://fresh"
    }

    rollback("myapp", store=store)

    env = fake_docker["create"].call_args.kwargs["env_vars"]
    assert env["DATABASE_URL"] == "postgresql://fresh", (
        "database credentials must come from live provisioning, not the row"
    )
    assert env["NODE_ENV"] == "production", (
        "non-database environment must still be replayed from the row"
    )


def test_rollback_passes_the_replayed_config_to_the_database_manager(
    store, fake_docker, mocker
):
    """DatabaseManager would otherwise re-read apps/<app>/ and provision
    against built-in defaults regardless of what the deployment used.
    """
    from launchbox import deploy as deploy_module
    from launchbox.deploy import rollback

    db_config = json.loads(json.dumps(CONFIGURED))
    db_config["database"] = {"enabled": True, "type": "mysql",
                             "version": "8", "name": "myapp_db"}

    record_deployment(store, "1111111", config=db_config, env={})
    record_deployment(store, "2222222", config=RECORDED_CONFIG, env={})

    manager = mocker.patch.object(deploy_module, "DatabaseManager")
    manager.return_value.create_database_for_app.return_value = {}

    rollback("myapp", store=store)

    passed = manager.return_value.create_database_for_app.call_args.kwargs[
        "config"
    ]
    assert passed.get_database_config()["type"] == "mysql"


def test_rollback_does_not_construct_a_database_manager_when_disabled(
    store, two_deployments, fake_docker, mocker
):
    from launchbox import deploy as deploy_module
    from launchbox.deploy import rollback

    manager = mocker.patch.object(deploy_module, "DatabaseManager")

    rollback("myapp", store=store)

    manager.assert_not_called()


# ------------------------------------------------ immutable image resolution


def test_rollback_prefers_the_recorded_image_id_over_the_tag(store,
                                                             fake_docker):
    """A rollback target recorded as launchbox-myapp:latest resolves, today,
    to whatever the most recent build tagged -- i.e. the build being escaped,
    reported as a successful rollback.
    """
    from launchbox.deploy import rollback

    record_deployment(store, "1111111", config=RECORDED_CONFIG, env={},
                      image_tag="launchbox-myapp:latest",
                      image_id="sha256:oldimage")
    record_deployment(store, "2222222", config=RECORDED_CONFIG, env={},
                      image_tag="launchbox-myapp:latest",
                      image_id="sha256:newimage")

    rollback("myapp", store=store)

    assert fake_docker["create"].call_args.kwargs["image_ref"] == (
        "sha256:oldimage"
    )


def test_rollback_falls_back_to_the_tag_when_no_image_id_was_recorded(
    store, two_deployments, fake_docker
):
    from launchbox.deploy import rollback

    rollback("myapp", store=store)

    assert fake_docker["create"].call_args.kwargs["image_ref"] == (
        "launchbox-myapp:1111111"
    )


def test_rollback_refuses_a_moving_latest_tag_with_no_image_id(store,
                                                               fake_docker):
    from launchbox.deploy import rollback

    record_deployment(store, "1111111", config=RECORDED_CONFIG, env={},
                      image_tag="launchbox-myapp:latest")
    record_deployment(store, "2222222", config=RECORDED_CONFIG, env={})

    with pytest.raises(DeploymentError, match="moving"):
        rollback("myapp", store=store)

    fake_docker["create"].assert_not_called()
    fake_docker["write_route"].assert_not_called()
    assert store.current_container("myapp") == "myapp-2222222"


def test_rollback_carries_the_image_id_onto_the_new_row(store, fake_docker):
    from launchbox.deploy import rollback

    record_deployment(store, "1111111", config=RECORDED_CONFIG, env={},
                      image_id="sha256:oldimage")
    record_deployment(store, "2222222", config=RECORDED_CONFIG, env={},
                      image_id="sha256:newimage")

    rollback("myapp", store=store)

    assert store.list_deployments("myapp")[0]["image_id"] == "sha256:oldimage"


# ------------------------------------------------------- LIKE-escaped --commit


def test_rollback_with_a_wildcard_commit_matches_nothing(store, fake_docker):
    """`--commit` reaches a SQL LIKE pattern. Unescaped, `_` matches any
    single character, so this selected an arbitrary deployment.
    """
    from launchbox.deploy import rollback

    record_deployment(store, "1111111", config=RECORDED_CONFIG, env={})
    record_deployment(store, "2222222", config=RECORDED_CONFIG, env={})

    with pytest.raises(DeploymentError, match="no successful deployment"):
        rollback("myapp", commit_sha="_", store=store)

    fake_docker["create"].assert_not_called()


def test_rollback_with_a_genuine_prefix_still_resolves(store, fake_docker):
    from launchbox.deploy import rollback

    record_deployment(store, "1111111", config=RECORDED_CONFIG, env={})
    record_deployment(store, "2222222", config=RECORDED_CONFIG, env={})

    rollback("myapp", commit_sha="111", store=store)

    assert fake_docker["create"].call_args.kwargs["image_ref"] == (
        "launchbox-myapp:1111111"
    )
