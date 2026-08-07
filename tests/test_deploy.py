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
        "running": mocker.patch.object(
            deploy_module.runner, "container_is_running", return_value=False
        ),
        "find_containers": mocker.patch.object(
            deploy_module.runner, "find_app_containers", return_value=[]
        ),
        "image_id": mocker.patch.object(
            deploy_module.runner, "image_id",
            return_value="sha256:aaaa1111",
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


def test_container_creation_failure_is_recorded_not_left_in_progress(
    app_dir, store, fake_docker
):
    """A DeploymentError raised by create_container (e.g. docker run failing,
    or ensure_network failing) happens outside _promote's own probe-failure
    branch, so nothing records it unless the outer handler does. Before the
    fix this left the deployment row stuck at status='in_progress' forever.
    """
    from launchbox.deploy import deploy

    previous = store.record_start("myapp", "1111111")
    store.record_success(previous, "myapp-1111111", "launchbox-myapp:1111111")

    fake_docker["create"].side_effect = DeploymentError("port clash")

    with pytest.raises(DeploymentError):
        deploy("myapp", source_dir=app_dir, commit_sha="2222222", store=store)

    record = store.list_deployments("myapp")[0]
    assert record["status"] == "failed"
    assert record["error"]

    removed = [c[0][0] for c in fake_docker["remove"].call_args_list]
    assert "myapp-1111111" not in removed


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


def test_live_container_recovered_by_label_when_state_store_is_empty(
    app_dir, store, fake_docker
):
    """If the state row is missing (lost/reset database) but a container this
    pipeline created is still running, it must be found by its
    ``launchbox.app`` label and NOT force-removed before its replacement
    exists -- this is the defect the reviewer reproduced.
    """
    from launchbox.deploy import deploy

    # No state row for "myapp". A container matching the SHA being redeployed
    # is nonetheless live, discoverable only via the label lookup.
    fake_docker["find_containers"].return_value = ["myapp-2222222"]
    fake_docker["exists"].side_effect = lambda name: name == "myapp-2222222"
    fake_docker["running"].side_effect = lambda name: name == "myapp-2222222"

    call_order = []
    fake_docker["create"].side_effect = lambda **kwargs: (
        call_order.append(("create", kwargs["container_name"])) or "cid123"
    )
    fake_docker["remove"].side_effect = lambda name: (
        call_order.append(("remove", name)) or True
    )

    name = deploy("myapp", source_dir=app_dir, commit_sha="2222222", store=store)

    # The collision guard gave the replacement a distinct name rather than
    # colliding with the still-live container.
    assert name != "myapp-2222222"
    assert name.startswith("myapp-2222222-")

    ops = [op for op in call_order if op[0] == "create" or op[1] == "myapp-2222222"]
    create_index = next(i for i, op in enumerate(ops) if op[0] == "create")
    remove_index = next(
        i for i, op in enumerate(ops) if op == ("remove", "myapp-2222222")
    )
    assert create_index < remove_index, (
        "the live container must not be removed before its replacement is created"
    )


def test_label_lookup_prefers_running_container_over_stale_stopped_one(
    app_dir, store, fake_docker
):
    """docker ps -a lists newest-created first, and a stale, stopped
    container left behind by a deploy that died between create and cleanup
    (the exact case _promote's own pre-create guard exists for) can easily
    be newer than the container actually serving traffic. The running
    container must win regardless of listing order.
    """
    from launchbox.deploy import deploy

    # Newest-first, as docker ps -a orders them: the stale stopped container
    # was created after the still-running one.
    fake_docker["find_containers"].return_value = ["myapp-3333333", "myapp-2222222"]
    fake_docker["running"].side_effect = lambda name: name == "myapp-2222222"
    fake_docker["exists"].side_effect = lambda name: name in (
        "myapp-3333333", "myapp-2222222"
    )

    call_order = []
    fake_docker["create"].side_effect = lambda **kwargs: (
        call_order.append(("create", kwargs["container_name"])) or "cid123"
    )
    fake_docker["remove"].side_effect = lambda name: (
        call_order.append(("remove", name)) or True
    )

    name = deploy("myapp", source_dir=app_dir, commit_sha="2222222", store=store)

    # The collision guard fired, proving "myapp-2222222" (the running one)
    # was correctly identified as "previous", not the newer stopped one.
    assert name != "myapp-2222222"

    ops = [op for op in call_order if op[0] == "create" or op[1] == "myapp-2222222"]
    create_index = next(i for i, op in enumerate(ops) if op[0] == "create")
    remove_index = next(
        i for i, op in enumerate(ops) if op == ("remove", "myapp-2222222")
    )
    assert create_index < remove_index, (
        "the live container must not be removed before its replacement is created"
    )


def test_label_lookup_falls_back_to_a_stopped_container_when_none_running(
    app_dir, store, fake_docker
):
    """If nothing labelled for the app is currently running (a fully stopped
    app being redeployed), the second pass must still adopt a stopped
    candidate as 'previous' so it gets cleaned up, instead of losing track of
    it and leaving it running alongside the new container.
    """
    from launchbox.deploy import deploy

    fake_docker["find_containers"].return_value = ["myapp-3333333", "myapp-1111111"]
    fake_docker["running"].return_value = False
    fake_docker["exists"].side_effect = lambda name: name in (
        "myapp-3333333", "myapp-1111111"
    )

    deploy("myapp", source_dir=app_dir, commit_sha="2222222", store=store)

    removed = [c[0][0] for c in fake_docker["remove"].call_args_list]
    assert "myapp-3333333" in removed, (
        "a fully stopped app must still be cleaned up on redeploy"
    )


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
    mocker.patch.object(deploy_module.runner, "find_app_containers", return_value=[])
    mocker.patch.object(deploy_module.runner, "image_id", return_value=None)
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
    mocker.patch.object(deploy_module.runner, "find_app_containers", return_value=[])
    mocker.patch.object(deploy_module.runner, "image_id", return_value=None)
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


# ------------------------------------------- resolved configuration is captured


def test_deploy_stores_the_resolved_config_on_the_deployment_row(
    app_dir, store, fake_docker
):
    """Rollback replays this. Without it, rollback re-read apps/<app>/ --
    which for a git-pushed application never exists -- and LaunchboxConfig
    silently returns built-in defaults there rather than raising.
    """
    import json

    from launchbox.deploy import deploy

    deploy("myapp", source_dir=app_dir, commit_sha="abc1234", store=store)

    row = store.list_deployments("myapp")[0]
    stored = json.loads(row["config_json"])
    assert stored["app"]["port"] == 3000
    assert stored["app"]["health_check"]["path"] == "/health"


def test_deploy_stores_the_resolved_environment_on_the_deployment_row(
    tmp_path, store, mocker, tmp_dynamic_dir
):
    import json

    from launchbox import deploy as deploy_module

    d = tmp_path / "envapp"
    d.mkdir()
    (d / "Dockerfile").write_text("FROM scratch\n")
    (d / "launchbox.yaml").write_text(
        "app:\n  port: 3000\nenvironment:\n  NODE_ENV: production\n"
    )

    mocker.patch.object(deploy_module.builder, "build",
                        return_value="launchbox-envapp:abc1234")
    mocker.patch.object(deploy_module.runner, "create_container",
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
    mocker.patch.object(deploy_module.router, "write_route", return_value="path")

    deploy_module.deploy("envapp", source_dir=str(d), commit_sha="abc1234",
                         store=store)

    row = store.list_deployments("envapp")[0]
    assert json.loads(row["env_json"])["NODE_ENV"] == "production"


def test_deploy_captures_config_even_when_the_promotion_later_fails(
    app_dir, store, fake_docker
):
    fake_docker["health"].return_value = False

    from launchbox.deploy import deploy

    with pytest.raises(DeploymentError):
        deploy("myapp", source_dir=app_dir, commit_sha="2222222", store=store)

    assert store.list_deployments("myapp")[0]["config_json"] is not None


def test_deploy_records_the_immutable_image_id(app_dir, store, fake_docker):
    """launchbox-<app>:latest is re-pointed by every later build, so the tag
    alone cannot identify the image a deployment ran.
    """
    from launchbox.deploy import deploy

    deploy("myapp", source_dir=app_dir, commit_sha="abc1234", store=store)

    assert store.list_deployments("myapp")[0]["image_id"] == "sha256:aaaa1111"
    fake_docker["image_id"].assert_called_once_with("launchbox-myapp:abc1234")


# ------------------------------------------------- Traefik route propagation


def test_route_propagation_wait_happens_between_route_write_and_removal(
    app_dir, store, fake_docker, mocker
):
    """Traefik's file provider debounces reloads (providersThrottleDuration,
    default 2s), so for a moment after write_route the live routing table
    still names the OLD container. Removing it in that window produces 502s.
    """
    from launchbox import deploy as deploy_module

    previous = store.record_start("myapp", "1111111")
    store.record_success(previous, "myapp-1111111", "launchbox-myapp:1111111")

    call_order = []
    fake_docker["write_route"].side_effect = lambda *a, **k: (
        call_order.append("route") or "path"
    )
    mocker.patch.object(
        deploy_module, "_await_route_propagation",
        side_effect=lambda: call_order.append("wait"),
    )
    fake_docker["remove"].side_effect = lambda name: (
        call_order.append(f"remove:{name}") or True
    )

    deploy_module.deploy("myapp", source_dir=app_dir, commit_sha="2222222",
                         store=store)

    assert "route" in call_order and "wait" in call_order
    assert call_order.index("route") < call_order.index("wait")
    assert call_order.index("wait") < call_order.index("remove:myapp-1111111")


def test_await_route_propagation_sleeps_the_configured_time(mocker,
                                                            monkeypatch):
    from launchbox import deploy as deploy_module

    monkeypatch.setenv("LAUNCHBOX_ROUTE_PROPAGATION", "1.5")
    sleep = mocker.patch.object(deploy_module.time, "sleep")

    deploy_module._await_route_propagation()

    sleep.assert_called_once_with(1.5)


def test_route_propagation_default_covers_the_traefik_throttle_window():
    """Traefik's providersThrottleDuration defaults to 2s."""
    from launchbox import deploy as deploy_module

    assert deploy_module.ROUTE_PROPAGATION_SECONDS > 2.0


def test_route_propagation_default_is_used_without_the_override(mocker,
                                                                monkeypatch):
    from launchbox import deploy as deploy_module

    monkeypatch.delenv("LAUNCHBOX_ROUTE_PROPAGATION", raising=False)
    sleep = mocker.patch.object(deploy_module.time, "sleep")

    deploy_module._await_route_propagation()

    sleep.assert_called_once_with(deploy_module.ROUTE_PROPAGATION_SECONDS)


def test_route_propagation_can_be_disabled(mocker, monkeypatch):
    from launchbox import deploy as deploy_module

    monkeypatch.setenv("LAUNCHBOX_ROUTE_PROPAGATION", "0")
    sleep = mocker.patch.object(deploy_module.time, "sleep")

    deploy_module._await_route_propagation()

    sleep.assert_not_called()


def test_route_propagation_ignores_an_unparseable_override(monkeypatch):
    from launchbox import deploy as deploy_module

    monkeypatch.setenv("LAUNCHBOX_ROUTE_PROPAGATION", "soon")
    assert deploy_module._route_propagation_seconds() == (
        deploy_module.ROUTE_PROPAGATION_SECONDS
    )


# ------------------------------------------------------------ orphan reaping


def test_successful_deploy_reaps_the_previous_container_and_any_orphan(
    app_dir, store, fake_docker
):
    """Only the single container adopted as `previous` was ever removed. An
    orphan left behind by a deploy whose write_route raised stayed running and
    unrouted; a later deploy could then adopt it as `previous` and remove it
    instead of the container actually serving, leaking that one forever.
    """
    from launchbox.deploy import deploy

    previous_id = store.record_start("myapp", "1111111")
    store.record_success(previous_id, "myapp-1111111",
                         "launchbox-myapp:1111111")

    fake_docker["exists"].side_effect = lambda name: name in (
        "myapp-1111111", "myapp-0000000"
    )
    fake_docker["find_containers"].return_value = [
        "myapp-2222222", "myapp-1111111", "myapp-0000000",
    ]

    call_order = []
    fake_docker["create"].side_effect = lambda **kwargs: (
        call_order.append(("create", kwargs["container_name"])) or "cid123"
    )
    fake_docker["remove"].side_effect = lambda name: (
        call_order.append(("remove", name)) or True
    )

    name = deploy("myapp", source_dir=app_dir, commit_sha="2222222",
                  store=store)

    created_at = next(i for i, op in enumerate(call_order) if op[0] == "create")
    removed_after_create = [
        op[1] for op in call_order[created_at:] if op[0] == "remove"
    ]

    assert "myapp-1111111" in removed_after_create, (
        "the previous container must go"
    )
    assert "myapp-0000000" in removed_after_create, (
        "the unrouted orphan must go too"
    )
    assert name not in removed_after_create, (
        "the container just promoted must survive its own reap"
    )


def test_reaping_never_removes_the_newly_promoted_container(app_dir, store,
                                                            fake_docker):
    """The reap lists containers by label, and the newly promoted one carries
    that label too. It must be excluded by name.
    """
    from launchbox.deploy import deploy

    fake_docker["find_containers"].return_value = ["myapp-abc1234"]

    call_order = []
    fake_docker["create"].side_effect = lambda **kwargs: (
        call_order.append(("create", kwargs["container_name"])) or "cid123"
    )
    fake_docker["remove"].side_effect = lambda name: (
        call_order.append(("remove", name)) or True
    )

    name = deploy("myapp", source_dir=app_dir, commit_sha="abc1234",
                  store=store)

    created_at = next(i for i, op in enumerate(call_order) if op[0] == "create")
    removed_after_create = [
        op[1] for op in call_order[created_at:] if op[0] == "remove"
    ]

    assert name == "myapp-abc1234"
    assert removed_after_create == []
    assert store.current_container("myapp") == "myapp-abc1234"
