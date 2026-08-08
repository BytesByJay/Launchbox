"""Tests for updating an application's environment without rebuilding.

update_env() redeploys the currently running image with new environment
variables through the same health-gated promotion path as rollback, so it
shares that module's fixtures and conventions closely.
"""

import json

import pytest

from launchbox.logger import DeploymentError
from launchbox.state import StateStore

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
def store(tmp_state_db):
    s = StateStore(tmp_state_db)
    yield s
    s.close()


@pytest.fixture
def deployed(store):
    """A single running deployment with one custom env var already set."""
    record_deployment(
        store, "abc1234", config=RECORDED_CONFIG,
        env={"NODE_ENV": "production"},
        image_id="sha256:deadbeef",
    )
    return store


@pytest.fixture
def fake_docker(mocker):
    from launchbox import deploy as deploy_module

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
        "write_route": mocker.patch.object(
            deploy_module.router, "write_route", return_value="path"
        ),
        "build": mocker.patch.object(deploy_module.builder, "build"),
    }


# ---------------------------------------------------------------- happy path


def test_update_env_sets_a_new_variable(store, deployed, fake_docker):
    from launchbox.deploy import update_env

    name = update_env("myapp", set_vars={"DEBUG": "true"}, store=store)

    assert name.startswith("myapp-abc1234")
    env = fake_docker["create"].call_args.kwargs["env_vars"]
    assert env["DEBUG"] == "true"
    assert env["NODE_ENV"] == "production", "existing vars must be preserved"


def test_update_env_overwrites_an_existing_variable(store, deployed,
                                                     fake_docker):
    from launchbox.deploy import update_env

    update_env("myapp", set_vars={"NODE_ENV": "staging"}, store=store)

    env = fake_docker["create"].call_args.kwargs["env_vars"]
    assert env["NODE_ENV"] == "staging"


def test_update_env_unsets_a_variable(store, deployed, fake_docker):
    from launchbox.deploy import update_env

    update_env("myapp", unset_vars=["NODE_ENV"], store=store)

    env = fake_docker["create"].call_args.kwargs["env_vars"]
    assert "NODE_ENV" not in env


def test_update_env_never_rebuilds(store, deployed, fake_docker):
    from launchbox.deploy import update_env

    update_env("myapp", set_vars={"DEBUG": "true"}, store=store)

    fake_docker["build"].assert_not_called()


def test_update_env_records_a_new_deployment_row(store, deployed,
                                                  fake_docker):
    from launchbox.deploy import update_env

    update_env("myapp", set_vars={"DEBUG": "true"}, store=store)

    history = store.list_deployments("myapp")
    assert len(history) == 2
    assert history[0]["status"] == "success"
    assert history[0]["commit_sha"] == "abc1234"


def test_update_env_replaces_the_running_container(store, deployed,
                                                    fake_docker):
    from launchbox.deploy import update_env

    update_env("myapp", set_vars={"DEBUG": "true"}, store=store)

    removed = [c.args[0] for c in fake_docker["remove"].call_args_list]
    assert "myapp-abc1234" in removed


# ------------------------------------------------------------ reserved keys


@pytest.mark.parametrize("key", [
    "DATABASE_URL", "DB_HOST", "DB_PORT", "DB_NAME", "DB_USER",
    "DB_PASSWORD", "DB_TYPE",
])
def test_update_env_rejects_setting_a_reserved_key(store, deployed,
                                                    fake_docker, key):
    from launchbox.deploy import update_env

    with pytest.raises(ValueError, match=key):
        update_env("myapp", set_vars={key: "hack"}, store=store)

    fake_docker["create"].assert_not_called()


def test_update_env_rejects_unsetting_a_reserved_key(store, deployed,
                                                      fake_docker):
    from launchbox.deploy import update_env

    with pytest.raises(ValueError, match="DB_PASSWORD"):
        update_env("myapp", unset_vars=["DB_PASSWORD"], store=store)

    fake_docker["create"].assert_not_called()


def test_update_env_reserved_key_check_runs_before_touching_the_store(
    store, deployed, fake_docker
):
    """The rejection must be immediate, not discovered mid-promotion."""
    from launchbox.deploy import update_env

    with pytest.raises(ValueError):
        update_env("myapp", set_vars={"DB_PASSWORD": "hack"}, store=store)

    assert len(store.list_deployments("myapp")) == 1, (
        "a rejected request must not create a deployment row"
    )


# --------------------------------------------------------------- refusals


def test_update_env_refuses_when_there_is_no_current_deployment(store,
                                                                 fake_docker):
    from launchbox.deploy import update_env

    with pytest.raises(DeploymentError, match="no current deployment"):
        update_env("myapp", set_vars={"DEBUG": "true"}, store=store)

    fake_docker["create"].assert_not_called()


def test_update_env_refuses_when_config_was_never_captured(store,
                                                            fake_docker):
    from launchbox.deploy import update_env

    record_deployment(store, "abc1234", config=None, env={})

    with pytest.raises(DeploymentError, match="predates"):
        update_env("myapp", set_vars={"DEBUG": "true"}, store=store)

    fake_docker["create"].assert_not_called()


def test_update_env_refuses_a_moving_latest_tag_with_no_image_id(
    store, fake_docker
):
    from launchbox.deploy import update_env

    record_deployment(store, None, config=RECORDED_CONFIG, env={},
                      image_tag="launchbox-myapp:latest")

    with pytest.raises(DeploymentError, match="moving"):
        update_env("myapp", set_vars={"DEBUG": "true"}, store=store)

    fake_docker["create"].assert_not_called()


def test_update_env_refuses_when_the_image_is_gone(store, deployed,
                                                    fake_docker):
    from launchbox.deploy import update_env

    fake_docker["image_exists"].return_value = False

    with pytest.raises(DeploymentError, match="image"):
        update_env("myapp", set_vars={"DEBUG": "true"}, store=store)

    fake_docker["create"].assert_not_called()


def test_update_env_rejects_an_invalid_app_name(store, fake_docker):
    from launchbox.deploy import update_env

    with pytest.raises(ValueError):
        update_env("../etc", set_vars={"DEBUG": "true"}, store=store)


# ------------------------------------------------------ failed probe safety


def test_failed_probe_leaves_the_current_version_running(store, deployed,
                                                          fake_docker):
    """The headline guarantee: an environment update that fails its own
    health probe must not touch the currently running container.
    """
    from launchbox.deploy import update_env

    fake_docker["health"].return_value = False

    with pytest.raises(DeploymentError, match="health"):
        update_env("myapp", set_vars={"DEBUG": "true"}, store=store)

    removed = [c.args[0] for c in fake_docker["remove"].call_args_list]
    assert "myapp-abc1234" not in removed
    fake_docker["write_route"].assert_not_called()
    assert store.current_container("myapp") == "myapp-abc1234"


def test_failed_probe_is_recorded_as_failed_not_left_in_progress(
    store, deployed, fake_docker
):
    from launchbox.deploy import update_env

    fake_docker["health"].return_value = False

    with pytest.raises(DeploymentError):
        update_env("myapp", set_vars={"DEBUG": "true"}, store=store)

    history = store.list_deployments("myapp")
    assert history[0]["status"] == "failed"


# ----------------------------------------------------------- database safety


def test_update_env_re_provisions_database_credentials_live(store,
                                                             fake_docker,
                                                             mocker):
    """Even though database env is excluded from set/unset, the base
    environment it is merged into still comes from live provisioning, not
    from whatever was last recorded -- mirrors rollback's guarantee.
    """
    from launchbox import deploy as deploy_module
    from launchbox.deploy import update_env

    db_config = json.loads(json.dumps(RECORDED_CONFIG))
    db_config["database"]["enabled"] = True

    record_deployment(
        store, "abc1234", config=db_config,
        env={"NODE_ENV": "production", "DATABASE_URL": "postgresql://stale"},
    )

    manager = mocker.patch.object(deploy_module, "DatabaseManager")
    manager.return_value.create_database_for_app.return_value = {
        "DATABASE_URL": "postgresql://fresh"
    }

    update_env("myapp", set_vars={"DEBUG": "true"}, store=store)

    env = fake_docker["create"].call_args.kwargs["env_vars"]
    assert env["DATABASE_URL"] == "postgresql://fresh"
    assert env["DEBUG"] == "true"
    assert env["NODE_ENV"] == "production"


# ---------------------------------------------------------------------- CLI


def test_cli_env_set_passes_parsed_key_value_pairs(mocker):
    from launchbox import __main__ as cli

    update_env = mocker.patch.object(cli, "update_env",
                                     return_value="myapp-abc1234")

    exit_code = cli.main(["env", "myapp", "--set", "DEBUG=true",
                          "--set", "LOG_LEVEL=info"])

    assert exit_code == 0
    update_env.assert_called_once_with(
        "myapp", set_vars={"DEBUG": "true", "LOG_LEVEL": "info"},
        unset_vars=[],
    )


def test_cli_env_unset_is_repeatable(mocker):
    from launchbox import __main__ as cli

    update_env = mocker.patch.object(cli, "update_env",
                                     return_value="myapp-abc1234")

    cli.main(["env", "myapp", "--unset", "DEBUG", "--unset", "LOG_LEVEL"])

    update_env.assert_called_once_with(
        "myapp", set_vars={}, unset_vars=["DEBUG", "LOG_LEVEL"]
    )


def test_cli_env_set_and_unset_together(mocker):
    from launchbox import __main__ as cli

    update_env = mocker.patch.object(cli, "update_env",
                                     return_value="myapp-abc1234")

    cli.main(["env", "myapp", "--set", "DEBUG=true", "--unset", "STALE_VAR"])

    update_env.assert_called_once_with(
        "myapp", set_vars={"DEBUG": "true"}, unset_vars=["STALE_VAR"]
    )


def test_cli_env_rejects_a_malformed_set_value(mocker):
    from launchbox import __main__ as cli

    update_env = mocker.patch.object(cli, "update_env")

    exit_code = cli.main(["env", "myapp", "--set", "NOEQUALSIGN"])

    assert exit_code == 1
    update_env.assert_not_called()


def test_cli_env_value_may_itself_contain_an_equals_sign(mocker):
    """KEY=VALUE splits on the FIRST '=' only."""
    from launchbox import __main__ as cli

    update_env = mocker.patch.object(cli, "update_env",
                                     return_value="myapp-abc1234")

    cli.main(["env", "myapp", "--set", "CONNECTION_STRING=a=b=c"])

    update_env.assert_called_once_with(
        "myapp", set_vars={"CONNECTION_STRING": "a=b=c"}, unset_vars=[]
    )


def test_cli_env_reports_a_reserved_key_error_cleanly(mocker):
    from launchbox import __main__ as cli

    mocker.patch.object(cli, "update_env",
                        side_effect=ValueError("DB_PASSWORD are database "
                                              "connection variables"))

    assert cli.main(["env", "myapp", "--set", "DB_PASSWORD=hack"]) == 1


def test_cli_env_returns_nonzero_on_deployment_error(mocker):
    from launchbox import __main__ as cli

    mocker.patch.object(cli, "update_env",
                        side_effect=DeploymentError("health probe failed"))

    assert cli.main(["env", "myapp", "--set", "DEBUG=true"]) == 1
