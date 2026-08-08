"""Dashboard and application-removal tests.

Deployments name containers ``<app>-<sha7>``. The dashboard still looked
containers up by the bare application name, so every app read as "not
deployed", stop/start/logs all 404'd, and the remove endpoint removed nothing
while leaving the route file, the SHA-tagged images and the state row behind.
"""

import json
import os

import pytest

from launchbox import router
from launchbox.state import StateStore


@pytest.fixture
def store(tmp_state_db):
    s = StateStore(tmp_state_db)
    yield s
    s.close()


@pytest.fixture
def deployed(store):
    """An application whose live container is named <app>-<sha7>."""
    dep = store.record_start("myapp", "abc1234", config_json=json.dumps({}))
    store.record_success(dep, "myapp-abc1234", "launchbox-myapp:abc1234")
    return store


class _FakeContainer:
    def __init__(self, name):
        self.name = name
        self.id = "0123456789abcdef" * 4
        self.status = "running"
        self.attrs = {"Created": "2026-01-01T00:00:00Z",
                      "State": {"StartedAt": "2026-01-01T00:00:00Z"},
                      "NetworkSettings": {"Ports": {}}}
        self.labels = {}
        self.stopped = False
        self.started = False

    def stop(self):
        self.stopped = True

    def start(self):
        self.started = True

    def logs(self, tail=100):
        return b"hello from the real container"


class _FakeContainers:
    """Only the SHA-named container exists, as on a real deployment."""

    def __init__(self, known):
        self.known = known
        self.requested = []

    def get(self, name):
        import docker

        self.requested.append(name)
        if name not in self.known:
            raise docker.errors.NotFound(f"no such container: {name}")
        return self.known[name]


class _FakeClient:
    def __init__(self, known):
        self.containers = _FakeContainers(known)

    def info(self):
        return {}


@pytest.fixture
def fake_client(mocker):
    from launchbox import dashboard as dashboard_module

    client = _FakeClient({"myapp-abc1234": _FakeContainer("myapp-abc1234")})
    mocker.patch.object(dashboard_module, "get_docker_client",
                        return_value=client)
    return client


@pytest.fixture
def dashboard_state(mocker, deployed):
    """Point the dashboard's StateStore at the test database."""
    from launchbox import dashboard as dashboard_module

    mocker.patch.object(
        dashboard_module, "StateStore", lambda *a, **k: StateStore(
            deployed.db_path
        )
    )
    return deployed


@pytest.fixture
def client(mocker, tmp_path):
    from launchbox import dashboard as dashboard_module

    apps_dir = tmp_path / "apps"
    (apps_dir / "myapp").mkdir(parents=True)
    (apps_dir / "myapp" / "Dockerfile").write_text("FROM scratch\n")
    (apps_dir / "myapp" / "launchbox.yaml").write_text("app:\n  port: 3000\n")
    mocker.patch.object(dashboard_module, "APPS_DIR", str(apps_dir))

    dashboard_module.app.config["TESTING"] = True
    return dashboard_module.app.test_client()


# ---------------------------------------------------- container name resolution


def test_resolve_container_name_uses_the_state_store(dashboard_state):
    from launchbox.dashboard import resolve_container_name

    assert resolve_container_name("myapp") == "myapp-abc1234"


def test_resolve_container_name_falls_back_to_the_bare_name(mocker,
                                                            tmp_state_db):
    """Containers created before the naming scheme changed are named <app>."""
    from launchbox import dashboard as dashboard_module

    mocker.patch.object(dashboard_module, "StateStore",
                        lambda *a, **k: StateStore(tmp_state_db))

    assert dashboard_module.resolve_container_name("legacyapp") == "legacyapp"


def test_resolve_container_name_survives_an_unreadable_state_store(mocker):
    from launchbox import dashboard as dashboard_module

    mocker.patch.object(dashboard_module, "StateStore",
                        side_effect=RuntimeError("database is locked"))

    assert dashboard_module.resolve_container_name("myapp") == "myapp"


# --------------------------------------------------------------- app listing


def test_app_list_finds_a_deployed_app_by_its_sha_named_container(
    client, fake_client, dashboard_state
):
    response = client.get("/api/apps")

    assert response.status_code == 200
    apps = response.get_json()
    myapp = next(a for a in apps if a["name"] == "myapp")
    assert myapp["status"] == "running", (
        "a deployed app must not read as 'not deployed'"
    )
    assert "myapp-abc1234" in fake_client.containers.requested


def test_app_list_reports_not_deployed_when_nothing_is_running(
    client, mocker, dashboard_state
):
    from launchbox import dashboard as dashboard_module

    mocker.patch.object(dashboard_module, "get_docker_client",
                        return_value=_FakeClient({}))

    apps = client.get("/api/apps").get_json()
    assert apps[0]["status"] == "not deployed"


# ------------------------------------------------------- start / stop / logs


def test_stop_endpoint_targets_the_sha_named_container(client, fake_client,
                                                        dashboard_state):
    response = client.post("/api/apps/myapp/stop")

    assert response.status_code == 200
    assert fake_client.containers.requested == ["myapp-abc1234"]
    assert fake_client.containers.known["myapp-abc1234"].stopped is True


def test_start_endpoint_targets_the_sha_named_container(client, fake_client,
                                                         dashboard_state):
    response = client.post("/api/apps/myapp/start")

    assert response.status_code == 200
    assert fake_client.containers.known["myapp-abc1234"].started is True


def test_logs_endpoint_targets_the_sha_named_container(client, fake_client,
                                                        dashboard_state):
    response = client.get("/api/apps/myapp/logs")

    assert response.status_code == 200
    assert response.get_json()["logs"] == "hello from the real container"


def test_logs_endpoint_still_404s_for_an_app_with_no_container(
    client, mocker, dashboard_state
):
    from launchbox import dashboard as dashboard_module

    mocker.patch.object(dashboard_module, "get_docker_client",
                        return_value=_FakeClient({}))

    assert client.get("/api/apps/myapp/logs").status_code == 404


# ------------------------------------------------------- deploy / remove wiring


def test_deploy_endpoint_builds_only_once(client, mocker):
    """api_deploy_app called build() and then run(), and run() delegates to
    deploy(), which builds again -- every dashboard deploy built twice.
    """
    from launchbox import dashboard as dashboard_module

    deploy = mocker.patch.object(dashboard_module, "deploy",
                                 return_value="myapp-abc1234")

    response = client.post("/api/apps/myapp/deploy")

    assert response.status_code == 200
    deploy.assert_called_once_with("myapp")
    assert not hasattr(dashboard_module, "build"), (
        "the dashboard must no longer reach for builder.build directly"
    )


def test_remove_endpoint_delegates_to_remove_app(client, mocker):
    from launchbox import dashboard as dashboard_module

    remove = mocker.patch.object(
        dashboard_module, "remove_app",
        return_value={"app_name": "myapp", "route_removed": True,
                      "containers": ["myapp-abc1234"],
                      "images": ["launchbox-myapp:abc1234"],
                      "database_removed": False},
    )

    response = client.delete("/api/apps/myapp/remove")

    assert response.status_code == 200
    remove.assert_called_once_with("myapp")
    assert response.get_json()["removed"]["containers"] == ["myapp-abc1234"]


def test_remove_endpoint_rejects_an_invalid_app_name(client, mocker):
    from launchbox import dashboard as dashboard_module

    mocker.patch.object(dashboard_module, "remove_app",
                        side_effect=ValueError("Invalid application name"))

    assert client.delete("/api/apps/myapp/remove").status_code == 400


# ------------------------------------------------------------------ remove_app


@pytest.fixture
def fake_removal(mocker):
    from launchbox import deploy as deploy_module

    return {
        "find_containers": mocker.patch.object(
            deploy_module.runner, "find_app_containers",
            return_value=["myapp-abc1234"],
        ),
        "stop_and_remove": mocker.patch.object(
            deploy_module.runner, "stop_and_remove", return_value=True
        ),
        "list_images": mocker.patch.object(
            deploy_module.runner, "list_images",
            return_value=["launchbox-myapp:abc1234", "launchbox-myapp:latest"],
        ),
        "remove_image": mocker.patch.object(
            deploy_module.runner, "remove_image", return_value=True
        ),
    }


def test_remove_app_deletes_route_container_images_and_state_row(
    deployed, fake_removal, tmp_dynamic_dir
):
    from launchbox.deploy import remove_app

    route_path = router.write_route("myapp", "myapp-abc1234", 3000,
                                    dynamic_dir=tmp_dynamic_dir)
    assert os.path.exists(route_path)

    summary = remove_app("myapp", store=deployed, dynamic_dir=tmp_dynamic_dir)

    assert not os.path.exists(route_path), (
        "Traefik must not be left with a router aimed at a deleted backend"
    )
    assert summary["route_removed"] is True
    assert "myapp-abc1234" in summary["containers"]
    assert summary["images"] == [
        "launchbox-myapp:abc1234", "launchbox-myapp:latest"
    ]
    assert deployed.get_app("myapp") is None, (
        "a removed app must not still appear in `launchbox list`"
    )


def test_remove_app_removes_the_route_before_the_container(
    deployed, fake_removal, tmp_dynamic_dir, mocker
):
    """Ordering is the point: traffic must stop being routed at a container
    before that container disappears.
    """
    from launchbox import deploy as deploy_module
    from launchbox.deploy import remove_app

    router.write_route("myapp", "myapp-abc1234", 3000,
                       dynamic_dir=tmp_dynamic_dir)

    call_order = []
    real_remove_route = deploy_module.router.remove_route
    mocker.patch.object(
        deploy_module.router, "remove_route",
        side_effect=lambda name, dynamic_dir: (
            call_order.append("route") or real_remove_route(name, dynamic_dir)
        ),
    )
    fake_removal["stop_and_remove"].side_effect = lambda name: (
        call_order.append(f"container:{name}") or True
    )
    fake_removal["remove_image"].side_effect = lambda ref: (
        call_order.append(f"image:{ref}") or True
    )

    remove_app("myapp", store=deployed, dynamic_dir=tmp_dynamic_dir)

    assert call_order[0] == "route"
    assert call_order[1].startswith("container:")
    assert call_order[-1].startswith("image:")


def test_remove_app_also_removes_a_legacy_bare_named_container(
    deployed, fake_removal, tmp_dynamic_dir
):
    from launchbox.deploy import remove_app

    fake_removal["find_containers"].return_value = []

    summary = remove_app("myapp", store=deployed, dynamic_dir=tmp_dynamic_dir)

    assert "myapp" in summary["containers"]


def test_remove_app_leaves_the_database_alone_by_default(
    deployed, fake_removal, tmp_dynamic_dir, mocker
):
    """Removing an application must not silently destroy its data."""
    from launchbox import deploy as deploy_module
    from launchbox.deploy import remove_app

    manager = mocker.patch.object(deploy_module, "DatabaseManager")
    deployed.record_database("myapp", "postgresql", "myapp_postgres",
                             "myapp_vol", "myapp_db", "u", "p")

    remove_app("myapp", store=deployed, dynamic_dir=tmp_dynamic_dir)

    manager.assert_not_called()
    assert deployed.get_database("myapp") is not None


def test_remove_app_destroys_the_database_when_asked(
    deployed, fake_removal, tmp_dynamic_dir, mocker
):
    from launchbox import deploy as deploy_module
    from launchbox.deploy import remove_app

    manager = mocker.patch.object(deploy_module, "DatabaseManager")
    manager.return_value.remove_database_for_app.return_value = True
    deployed.record_database("myapp", "postgresql", "myapp_postgres",
                             "myapp_vol", "myapp_db", "u", "p")

    summary = remove_app("myapp", store=deployed, dynamic_dir=tmp_dynamic_dir,
                         remove_database=True)

    assert summary["database_removed"] is True
    assert deployed.get_database("myapp") is None


def test_remove_app_rejects_an_invalid_app_name(deployed, tmp_dynamic_dir):
    from launchbox.deploy import remove_app

    with pytest.raises(ValueError):
        remove_app("../etc", store=deployed, dynamic_dir=tmp_dynamic_dir)


def test_remove_app_is_tolerant_of_a_missing_route_file(
    deployed, fake_removal, tmp_dynamic_dir
):
    from launchbox.deploy import remove_app

    summary = remove_app("myapp", store=deployed, dynamic_dir=tmp_dynamic_dir)

    assert summary["route_removed"] is False
    assert deployed.get_app("myapp") is None


# ------------------------------------------------------------- CLI wiring


def test_cli_exposes_a_remove_subcommand():
    """Removing an application cleanly was previously impossible by any
    route: no CLI command, and the dashboard endpoint removed nothing.
    """
    from launchbox.__main__ import build_parser

    parser = build_parser()
    sub = next(a for a in parser._actions
               if hasattr(a, "choices") and a.choices)
    assert "remove" in sub.choices


def test_cli_remove_calls_remove_app(mocker):
    from launchbox import __main__ as cli

    remove = mocker.patch.object(
        cli, "remove_app",
        return_value={"app_name": "myapp", "route_removed": True,
                      "containers": ["myapp-abc1234"], "images": [],
                      "database_removed": False},
    )

    assert cli.main(["remove", "myapp"]) == 0
    remove.assert_called_once_with("myapp", remove_database=False)


def test_cli_remove_passes_the_database_flag(mocker):
    from launchbox import __main__ as cli

    remove = mocker.patch.object(
        cli, "remove_app",
        return_value={"app_name": "myapp", "route_removed": False,
                      "containers": [], "images": [],
                      "database_removed": True},
    )

    assert cli.main(["remove", "myapp", "--with-database"]) == 0
    remove.assert_called_once_with("myapp", remove_database=True)


def test_cli_remove_reports_an_invalid_app_name_cleanly(mocker):
    from launchbox import __main__ as cli

    assert cli.main(["remove", "bad name!"]) == 1


# ------------------------------------------------------------ detail page


def test_app_detail_page_renders_with_history(client, fake_client,
                                              dashboard_state):
    """This route 500'd until app_detail.html existed."""
    response = client.get('/app/myapp')

    assert response.status_code == 200
    body = response.get_data(as_text=True)
    assert 'myapp' in body
    assert 'abc1234' in body, "deployment history must be shown"


def test_app_detail_marks_the_current_deployment_not_rollback_eligible(
    client, fake_client, dashboard_state
):
    response = client.get('/app/myapp')
    body = response.get_data(as_text=True)

    assert 'Current' in body
    assert 'Roll back' not in body, (
        "the only recorded deployment is the current one; nothing to roll "
        "back to"
    )


def test_app_detail_offers_rollback_for_an_older_successful_deployment(
    client, fake_client, dashboard_state
):
    dep = dashboard_state.record_start("myapp", "0000000")
    dashboard_state.record_success(dep, "myapp-abc1234",
                                   "launchbox-myapp:abc1234")
    # myapp-abc1234 is deployed twice above: the fixture's initial deploy,
    # then this one. Add one more so an OLDER, non-current row exists.
    older = dashboard_state.record_start("myapp", "1111111")
    dashboard_state.record_success(older, "myapp-1111111",
                                   "launchbox-myapp:1111111")
    current = dashboard_state.record_start("myapp", "2222222")
    dashboard_state.record_success(current, "myapp-abc1234",
                                   "launchbox-myapp:2222222")

    response = client.get('/app/myapp')
    body = response.get_data(as_text=True)

    assert 'Roll back' in body
    assert '1111111' in body


def test_app_detail_404_app_flashes_and_redirects(client, dashboard_state):
    response = client.get('/app/nosuchapp', follow_redirects=True)

    assert response.status_code == 200
    assert b'not found' in response.data


# ------------------------------------------------------------- rollback


def test_rollback_endpoint_delegates_to_rollback(client, mocker):
    from launchbox import dashboard as dashboard_module

    rb = mocker.patch.object(dashboard_module, "rollback",
                             return_value="myapp-1111111")

    response = client.post('/api/apps/myapp/rollback',
                           json={'commit_sha': '1111111'})

    assert response.status_code == 200
    rb.assert_called_once_with('myapp', commit_sha='1111111')
    assert response.get_json()['container'] == 'myapp-1111111'


def test_rollback_endpoint_works_with_no_body(client, mocker):
    """The dashboard's own button never sends a commit_sha explicitly for
    the 'roll back to last good version' case in other entry points, so the
    endpoint must tolerate an empty or missing JSON body.
    """
    from launchbox import dashboard as dashboard_module

    rb = mocker.patch.object(dashboard_module, "rollback",
                             return_value="myapp-1111111")

    response = client.post('/api/apps/myapp/rollback')

    assert response.status_code == 200
    rb.assert_called_once_with('myapp', commit_sha=None)


def test_rollback_endpoint_rejects_an_invalid_app_name(client, mocker):
    from launchbox import dashboard as dashboard_module

    mocker.patch.object(dashboard_module, "rollback",
                        side_effect=ValueError("Invalid application name"))

    response = client.post('/api/apps/bad name!/rollback')
    assert response.status_code == 400


def test_rollback_endpoint_reports_a_failed_health_probe(client, mocker):
    from launchbox import dashboard as dashboard_module
    from launchbox.logger import DeploymentError

    mocker.patch.object(dashboard_module, "rollback",
                        side_effect=DeploymentError("health probe failed"))

    response = client.post('/api/apps/myapp/rollback',
                           json={'commit_sha': 'abc1234'})

    assert response.status_code == 500
    assert 'health probe failed' in response.get_json()['error']


# ---------------------------------------------------------- health extraction


def test_health_label_prefers_the_healthcheck_status_when_running(
    client, mocker, dashboard_state
):
    from launchbox import dashboard as dashboard_module

    container = _FakeContainer("myapp-abc1234")
    container.attrs["State"]["Health"] = {"Status": "unhealthy"}
    mocker.patch.object(dashboard_module, "get_docker_client",
                        return_value=_FakeClient({"myapp-abc1234": container}))

    apps = client.get('/api/apps').get_json()
    myapp = next(a for a in apps if a["name"] == "myapp")

    assert myapp["health_status"] == "unhealthy"
    assert myapp["status"] == "running", (
        "Docker's run state and the HEALTHCHECK state are different things"
    )


def test_health_label_ignores_stale_health_on_a_stopped_container(
    client, mocker, dashboard_state
):
    """A container's last-known Health.Status survives after it stops; that
    stale value must never override the fact that it is not running.
    """
    from launchbox import dashboard as dashboard_module

    container = _FakeContainer("myapp-abc1234")
    container.status = "exited"
    container.attrs["State"]["Health"] = {"Status": "unhealthy"}
    mocker.patch.object(dashboard_module, "get_docker_client",
                        return_value=_FakeClient({"myapp-abc1234": container}))

    apps = client.get('/api/apps').get_json()
    myapp = next(a for a in apps if a["name"] == "myapp")

    assert myapp["health_status"] == "exited"


def test_health_label_is_healthy_for_a_passing_probe(client, fake_client,
                                                      dashboard_state):
    fake_client.containers.known["myapp-abc1234"].attrs["State"]["Health"] = {
        "Status": "healthy"
    }

    apps = client.get('/api/apps').get_json()
    myapp = next(a for a in apps if a["name"] == "myapp")

    assert myapp["health_status"] == "healthy"


def test_health_label_is_running_for_a_container_with_no_healthcheck(
    client, fake_client, dashboard_state
):
    apps = client.get('/api/apps').get_json()
    myapp = next(a for a in apps if a["name"] == "myapp")

    assert myapp["health_status"] == "running"


# ------------------------------------------------------------ git-push visibility


def test_app_list_includes_a_git_push_only_app(client, fake_client, deployed,
                                               mocker):
    """An app deployed purely via `git push` has no apps/<name> directory on
    the Launchbox host -- it must still appear.
    """
    from launchbox import dashboard as dashboard_module

    deployed.register("gitpushapp")
    mocker.patch.object(
        dashboard_module, "StateStore",
        lambda *a, **k: StateStore(deployed.db_path)
    )

    apps = client.get('/api/apps').get_json()
    names = [a["name"] for a in apps]

    assert "gitpushapp" in names
    ghost = next(a for a in apps if a["name"] == "gitpushapp")
    assert ghost["has_local_dir"] is False
    assert ghost["status"] == "not deployed"


def test_git_push_only_app_reads_its_config_from_the_deployment_row(
    client, fake_client, store, mocker
):
    from launchbox import dashboard as dashboard_module

    config_json = json.dumps({"app": {"port": 9000}, "https": {"enabled": True}})
    dep = store.record_start("gitpushapp", "abc1234", config_json=config_json)
    store.record_success(dep, "gitpushapp-abc1234",
                         "launchbox-gitpushapp:abc1234")
    mocker.patch.object(
        dashboard_module, "StateStore",
        lambda *a, **k: StateStore(store.db_path)
    )

    apps = client.get('/api/apps').get_json()
    ghost = next(a for a in apps if a["name"] == "gitpushapp")

    assert ghost["port"] == 9000
    assert ghost["url"] == "https://gitpushapp.localhost"


def test_locally_present_app_is_not_duplicated_by_the_state_union(
    client, fake_client, dashboard_state
):
    apps = client.get('/api/apps').get_json()
    names = [a["name"] for a in apps]

    assert names.count("myapp") == 1


# --------------------------------------------------------------- registration


def test_register_endpoint_delegates_to_init(client, mocker):
    from launchbox import dashboard as dashboard_module

    init_mock = mocker.patch.object(dashboard_module, "init_app",
                                    return_value="/data/repos/newapp.git")

    response = client.post('/api/apps', json={"name": "newapp",
                                              "branch": "main"})

    assert response.status_code == 201
    init_mock.assert_called_once_with("newapp", default_branch="main")
    body = response.get_json()
    assert body["repo_path"] == "/data/repos/newapp.git"
    assert any("git push launchbox main" in c for c in body["push_commands"])


def test_register_endpoint_defaults_branch_to_main(client, mocker):
    from launchbox import dashboard as dashboard_module

    init_mock = mocker.patch.object(dashboard_module, "init_app",
                                    return_value="/data/repos/newapp.git")

    client.post('/api/apps', json={"name": "newapp"})

    init_mock.assert_called_once_with("newapp", default_branch="main")


def test_register_endpoint_rejects_an_invalid_name(client, mocker):
    from launchbox import dashboard as dashboard_module

    mocker.patch.object(dashboard_module, "init_app",
                        side_effect=ValueError("Invalid application name"))

    response = client.post('/api/apps', json={"name": "bad name!"})

    assert response.status_code == 400


def test_register_endpoint_rejects_an_empty_name(client, mocker):
    from launchbox import dashboard as dashboard_module

    mocker.patch.object(dashboard_module, "init_app",
                        side_effect=ValueError("Invalid application name"))

    response = client.post('/api/apps', json={"name": ""})

    assert response.status_code == 400


# ------------------------------------------------------------------ database


def test_detail_page_shows_the_database_panel(client, fake_client, deployed,
                                              mocker):
    from launchbox import dashboard as dashboard_module

    deployed.record_database("myapp", "postgresql", "myapp_postgres",
                             "myapp_vol", "myapp_db", "lb_myapp", "s3cret")
    mocker.patch.object(dashboard_module, "StateStore",
                        lambda *a, **k: StateStore(deployed.db_path))

    response = client.get('/app/myapp')
    body = response.get_data(as_text=True)
    assert "myapp_postgres" in body
    assert "5432" in body
    assert "myapp_db" in body
    assert "s3cret" not in body, "the database password must never reach the browser"


def test_detail_page_offers_provisioning_when_no_database_exists(
    client, fake_client, dashboard_state
):
    """No database is provisioned, but the app has a current deployment, so
    the dashboard should offer to add one rather than show a populated panel.

    The `deployed` fixture only sets config_json (not env_json), unlike a
    real deploy() -- which always calls record_config with both. Set
    env_json explicitly so this exercises the realistic case.
    """
    dashboard_state._conn.execute(
        "UPDATE deployments SET env_json = ? WHERE app_name = ?",
        (json.dumps({}), "myapp"),
    )
    dashboard_state._conn.commit()

    response = client.get('/app/myapp')
    body = response.get_data(as_text=True)

    assert "No database provisioned" in body
    assert 'id="db-provision-btn"' in body
    assert 'id="db-detach-btn"' not in body, (
        "there is nothing to detach when no database exists"
    )


def test_detail_page_env_editor_absent_when_a_real_deploy_never_ran(
    client, fake_client, deployed
):
    """A deployment row lacking env_json (this fixture's default shape)
    correctly disables both the env editor and the database form, rather
    than silently showing an empty editor for data that was never resolved.
    """
    response = client.get('/app/myapp')
    body = response.get_data(as_text=True)

    assert 'id="env-save-btn"' not in body
    assert 'id="db-provision-btn"' not in body


# -------------------------------------------------------------- recent activity


def test_homepage_shows_recent_activity(client, fake_client, dashboard_state):
    response = client.get('/')
    body = response.get_data(as_text=True)

    assert "Recent activity" in body
    assert "myapp" in body


def test_homepage_activity_feed_survives_a_broken_state_store(
    client, fake_client, mocker
):
    from launchbox import dashboard as dashboard_module

    mocker.patch.object(dashboard_module, "StateStore",
                        side_effect=RuntimeError("database is locked"))

    response = client.get('/')

    assert response.status_code == 200


# ------------------------------------------------------------- env endpoint


def test_env_endpoint_delegates_to_update_env(client, mocker):
    from launchbox import dashboard as dashboard_module

    upd = mocker.patch.object(dashboard_module, "update_env",
                              return_value="myapp-abc1234")

    response = client.post('/api/apps/myapp/env',
                           json={"set": {"DEBUG": "true"}, "unset": ["OLD"]})

    assert response.status_code == 200
    upd.assert_called_once_with("myapp", set_vars={"DEBUG": "true"},
                                unset_vars=["OLD"])
    assert response.get_json()["container"] == "myapp-abc1234"


def test_env_endpoint_defaults_to_empty_set_and_unset(client, mocker):
    from launchbox import dashboard as dashboard_module

    upd = mocker.patch.object(dashboard_module, "update_env",
                              return_value="myapp-abc1234")

    client.post('/api/apps/myapp/env')

    upd.assert_called_once_with("myapp", set_vars={}, unset_vars=[])


def test_env_endpoint_rejects_a_reserved_key(client, mocker):
    from launchbox import dashboard as dashboard_module

    mocker.patch.object(dashboard_module, "update_env",
                        side_effect=ValueError(
                            "DB_PASSWORD are database connection variables"
                        ))

    response = client.post('/api/apps/myapp/env',
                           json={"set": {"DB_PASSWORD": "hack"}})

    assert response.status_code == 400


def test_env_endpoint_reports_a_failed_health_probe(client, mocker):
    from launchbox import dashboard as dashboard_module
    from launchbox.logger import DeploymentError

    mocker.patch.object(dashboard_module, "update_env",
                        side_effect=DeploymentError("health probe failed"))

    response = client.post('/api/apps/myapp/env',
                           json={"set": {"DEBUG": "true"}})

    assert response.status_code == 500
    assert 'health probe failed' in response.get_json()['error']


# ------------------------------------------------------- editable_env in detail


def test_detail_page_exposes_editable_env_excluding_database_keys(
    client, fake_client, store, mocker
):
    dep = store.record_start("myapp", "abc1234", config_json=json.dumps({}))
    store.record_success(
        dep, "myapp-abc1234", "launchbox-myapp:abc1234",
    )
    # env_json is set separately since record_success doesn't take it.
    store._conn.execute(
        "UPDATE deployments SET env_json = ? WHERE id = ?",
        (json.dumps({"NODE_ENV": "production",
                    "DATABASE_URL": "postgresql://secret"}), dep),
    )
    store._conn.commit()

    from launchbox import dashboard as dashboard_module
    mocker.patch.object(dashboard_module, "StateStore",
                        lambda *a, **k: StateStore(store.db_path))

    response = client.get('/app/myapp')
    body = response.get_data(as_text=True)
    assert 'NODE_ENV' in body
    assert 'production' in body
    assert 'DATABASE_URL' not in body, (
        "database-managed keys must not appear in the editable list"
    )


def test_detail_page_env_editor_disabled_when_never_deployed(client):
    # myapp is present via the client fixture's local apps/ dir but has no
    # deployment row at all in a fresh store, so there is nothing to redeploy
    # with an env change.
    response = client.get('/app/myapp')

    assert response.status_code == 200
    body = response.get_data(as_text=True)
    assert 'at least once' in body


# --------------------------------------------------------- database endpoints


def test_configure_database_endpoint_delegates_to_configure_database(
    client, mocker
):
    from launchbox import dashboard as dashboard_module

    cfg = mocker.patch.object(dashboard_module, "configure_database",
                              return_value="myapp-abc1234")

    response = client.post('/api/apps/myapp/database',
                           json={"engine": "postgresql", "version": "13",
                                "name": "mydb"})

    assert response.status_code == 201
    cfg.assert_called_once_with("myapp", "postgresql", version="13",
                                db_name="mydb")


def test_configure_database_endpoint_requires_an_engine(client, mocker):
    from launchbox import dashboard as dashboard_module

    cfg = mocker.patch.object(dashboard_module, "configure_database")

    response = client.post('/api/apps/myapp/database', json={})

    assert response.status_code == 400
    cfg.assert_not_called()


def test_configure_database_endpoint_rejects_an_unsupported_engine(
    client, mocker
):
    from launchbox import dashboard as dashboard_module

    mocker.patch.object(dashboard_module, "configure_database",
                        side_effect=ValueError("Unsupported database engine"))

    response = client.post('/api/apps/myapp/database',
                           json={"engine": "oracle"})

    assert response.status_code == 400


def test_configure_database_endpoint_reports_a_failed_health_probe(
    client, mocker
):
    from launchbox import dashboard as dashboard_module
    from launchbox.logger import DeploymentError

    mocker.patch.object(dashboard_module, "configure_database",
                        side_effect=DeploymentError("health probe failed"))

    response = client.post('/api/apps/myapp/database',
                           json={"engine": "postgresql"})

    assert response.status_code == 500


def test_detach_database_endpoint_delegates_to_detach_database(client, mocker):
    from launchbox import dashboard as dashboard_module

    detach = mocker.patch.object(dashboard_module, "detach_database",
                                 return_value="myapp-abc1234")

    response = client.delete('/api/apps/myapp/database')

    assert response.status_code == 200
    detach.assert_called_once_with("myapp")


def test_detach_database_endpoint_rejects_an_invalid_app_name(client, mocker):
    from launchbox import dashboard as dashboard_module

    mocker.patch.object(dashboard_module, "detach_database",
                        side_effect=ValueError("Invalid application name"))

    response = client.delete('/api/apps/bad name!/database')

    assert response.status_code == 400


# --------------------------------------------------- database visibility fix


def test_detail_page_shows_the_injected_env_var_names(client, fake_client,
                                                       deployed, mocker):
    """DATABASE_URL/DB_HOST/etc were filtered out of the env editor and
    never listed anywhere else either -- the panel now names them.
    """
    from launchbox import dashboard as dashboard_module

    deployed._conn.execute(
        "UPDATE deployments SET env_json = ? WHERE app_name = ?",
        (json.dumps({"NODE_ENV": "production",
                    "DATABASE_URL": "postgresql://x", "DB_HOST": "h"}),
         "myapp"),
    )
    deployed._conn.commit()
    deployed.record_database("myapp", "postgresql", "myapp_postgres", "",
                             "myapp_db", "launchbox", "launchbox123")
    mocker.patch.object(dashboard_module, "StateStore",
                        lambda *a, **k: StateStore(deployed.db_path))

    response = client.get('/app/myapp')
    body = response.get_data(as_text=True)

    assert "DATABASE_URL" in body
    assert "DB_HOST" in body
    assert "launchbox123" not in body, "the real password must never render"


def test_detail_page_shows_a_redacted_connection_string(client, fake_client,
                                                         deployed, mocker):
    from launchbox import dashboard as dashboard_module

    deployed.record_database("myapp", "postgresql", "myapp_postgres", "",
                             "myapp_db", "launchbox", "s3cret-password")
    mocker.patch.object(dashboard_module, "StateStore",
                        lambda *a, **k: StateStore(deployed.db_path))

    response = client.get('/app/myapp')
    body = response.get_data(as_text=True)

    assert "postgresql://launchbox:***@myapp_postgres:5432/myapp_db" in body
    assert "s3cret-password" not in body


def test_env_editor_note_only_lists_keys_actually_present(client, fake_client,
                                                           deployed, mocker):
    """An app with a database provisioned but HTTPS/etc not wired into its
    env should not claim keys it was never actually given.
    """
    from launchbox import dashboard as dashboard_module

    deployed._conn.execute(
        "UPDATE deployments SET env_json = ? WHERE app_name = ?",
        (json.dumps({"NODE_ENV": "production"}), "myapp"),
    )
    deployed._conn.commit()
    deployed.record_database("myapp", "postgresql", "myapp_postgres", "",
                             "myapp_db", "launchbox", "launchbox123")
    mocker.patch.object(dashboard_module, "StateStore",
                        lambda *a, **k: StateStore(deployed.db_path))

    response = client.get('/app/myapp')
    body = response.get_data(as_text=True)

    assert "Also set, not editable here" not in body, (
        "no reserved keys were actually injected for this app"
    )


# ------------------------------------------------- database credentials reveal


def test_credentials_endpoint_returns_the_real_password(client, deployed,
                                                         mocker):
    from launchbox import dashboard as dashboard_module

    deployed.record_database("myapp", "postgresql", "myapp_postgres", "",
                             "myapp_db", "launchbox", "s3cret-password")
    mocker.patch.object(dashboard_module, "StateStore",
                        lambda *a, **k: StateStore(deployed.db_path))

    response = client.get('/api/apps/myapp/database/credentials')

    assert response.status_code == 200
    body = response.get_json()
    assert body["password"] == "s3cret-password"
    assert body["username"] == "launchbox"


def test_credentials_endpoint_builds_a_usable_connection_url(client, deployed,
                                                              mocker):
    from launchbox import dashboard as dashboard_module

    deployed.record_database("myapp", "mysql", "myapp_mysql", "",
                             "myapp_db", "launchbox", "s3cret-password")
    mocker.patch.object(dashboard_module, "StateStore",
                        lambda *a, **k: StateStore(deployed.db_path))

    body = client.get('/api/apps/myapp/database/credentials').get_json()

    assert body["database_url"] == (
        "mysql://launchbox:s3cret-password@myapp_mysql:3306/myapp_db"
    )


def test_credentials_endpoint_404s_when_no_database_exists(client,
                                                            dashboard_state):
    response = client.get('/api/apps/myapp/database/credentials')

    assert response.status_code == 404


def test_the_password_is_still_absent_from_the_rendered_page(client,
                                                              fake_client,
                                                              deployed,
                                                              mocker):
    """Revealing is an explicit action. The password must not be baked into
    the page source, where it would land in the browser cache and any
    screenshot of the page.
    """
    from launchbox import dashboard as dashboard_module

    deployed.record_database("myapp", "postgresql", "myapp_postgres", "",
                             "myapp_db", "launchbox", "s3cret-password")
    mocker.patch.object(dashboard_module, "StateStore",
                        lambda *a, **k: StateStore(deployed.db_path))

    body = client.get('/app/myapp').get_data(as_text=True)

    assert "s3cret-password" not in body
    assert "postgresql://launchbox:***@" in body, "the redacted form still renders"


# ------------------------------------------- injected variable values on page


def test_non_secret_injected_values_render_on_the_page(client, fake_client,
                                                        deployed, mocker):
    """Listing only the variable NAMES left the values unreadable -- the
    host, port, database and user are not secrets and belong on the page.
    """
    from launchbox import dashboard as dashboard_module

    deployed._conn.execute(
        "UPDATE deployments SET env_json = ? WHERE app_name = ?",
        (json.dumps({
            "DATABASE_URL": "postgresql://launchbox:s3cret@myapp_postgres:5432/myapp_db",
            "DB_HOST": "myapp_postgres", "DB_PORT": "5432",
            "DB_NAME": "myapp_db", "DB_USER": "launchbox",
            "DB_PASSWORD": "s3cret", "DB_TYPE": "postgresql",
        }), "myapp"),
    )
    deployed._conn.commit()
    deployed.record_database("myapp", "postgresql", "myapp_postgres", "",
                             "myapp_db", "launchbox", "s3cret")
    mocker.patch.object(dashboard_module, "StateStore",
                        lambda *a, **k: StateStore(deployed.db_path))

    body = client.get('/app/myapp').get_data(as_text=True)

    assert "myapp_postgres" in body
    assert "5432" in body
    assert "postgresql" in body


def test_secret_injected_values_are_masked_on_the_page(client, fake_client,
                                                        deployed, mocker):
    """DB_PASSWORD and DATABASE_URL both carry the password, so neither may
    render until explicitly revealed.
    """
    from launchbox import dashboard as dashboard_module

    deployed._conn.execute(
        "UPDATE deployments SET env_json = ? WHERE app_name = ?",
        (json.dumps({
            "DATABASE_URL": "postgresql://launchbox:s3cret@myapp_postgres:5432/myapp_db",
            "DB_HOST": "myapp_postgres", "DB_PASSWORD": "s3cret",
        }), "myapp"),
    )
    deployed._conn.commit()
    deployed.record_database("myapp", "postgresql", "myapp_postgres", "",
                             "myapp_db", "launchbox", "s3cret")
    mocker.patch.object(dashboard_module, "StateStore",
                        lambda *a, **k: StateStore(deployed.db_path))

    body = client.get('/app/myapp').get_data(as_text=True)

    assert "s3cret" not in body


def test_credentials_endpoint_returns_every_injected_variable(client,
                                                               deployed,
                                                               mocker):
    from launchbox import dashboard as dashboard_module

    deployed.record_database("myapp", "postgresql", "myapp_postgres", "",
                             "myapp_db", "launchbox", "s3cret")
    mocker.patch.object(dashboard_module, "StateStore",
                        lambda *a, **k: StateStore(deployed.db_path))

    env = client.get('/api/apps/myapp/database/credentials').get_json()["env"]

    assert set(env) == {
        "DATABASE_URL", "DB_HOST", "DB_PORT", "DB_NAME", "DB_USER",
        "DB_PASSWORD", "DB_TYPE",
    }
    assert env["DB_PASSWORD"] == "s3cret"
    assert env["DB_PORT"] == "5432"
    assert env["DB_TYPE"] == "postgresql"


# ------------------------------------------------------------- UI structure


def test_detail_page_renders_all_four_tabs(client, fake_client,
                                            dashboard_state):
    """The detail page was one long scroll of stacked cards; every
    comparable tool sections a service page instead.
    """
    body = client.get('/app/myapp').get_data(as_text=True)

    for tab in ("overview", "environment", "database", "deployments"):
        assert f'data-tab="{tab}"' in body
        assert f'data-panel="{tab}"' in body


def test_app_list_reports_when_it_was_last_deployed(client, fake_client,
                                                     dashboard_state):
    apps = client.get('/api/apps').get_json()
    myapp = next(a for a in apps if a["name"] == "myapp")

    assert myapp["last_deployed_at"] is not None
    assert myapp["last_commit"] == "abc1234"


def test_app_list_last_deployed_is_none_for_a_never_deployed_app(client,
                                                                  store,
                                                                  mocker):
    from launchbox import dashboard as dashboard_module

    store.register("freshapp")
    mocker.patch.object(dashboard_module, "StateStore",
                        lambda *a, **k: StateStore(store.db_path))

    apps = client.get('/api/apps').get_json()
    fresh = next(a for a in apps if a["name"] == "freshapp")

    assert fresh["last_deployed_at"] is None
    assert fresh["last_commit"] is None


# ------------------------------------------------ deployment kind in the UI


def test_homepage_activity_feed_labels_the_kind_of_each_event(client,
                                                               fake_client,
                                                               store,
                                                               mocker):
    from launchbox import dashboard as dashboard_module

    dep = store.record_start("myapp", "abc1234", kind="rollback")
    store.record_success(dep, "myapp-abc1234", "launchbox-myapp:abc1234")
    mocker.patch.object(dashboard_module, "StateStore",
                        lambda *a, **k: StateStore(store.db_path))

    body = client.get('/').get_data(as_text=True)

    assert "rolled back" in body


def test_homepage_activity_feed_labels_a_plain_deploy(client, fake_client,
                                                       dashboard_state):
    body = client.get('/').get_data(as_text=True)

    assert "fa-rocket" in body
    assert "deployed" in body


def test_detail_page_history_table_shows_the_kind_column(client, fake_client,
                                                          store, mocker):
    from launchbox import dashboard as dashboard_module

    dep = store.record_start("myapp", "abc1234", kind="env")
    store.record_success(dep, "myapp-abc1234", "launchbox-myapp:abc1234")
    mocker.patch.object(dashboard_module, "StateStore",
                        lambda *a, **k: StateStore(store.db_path))

    body = client.get('/app/myapp').get_data(as_text=True)

    assert "Env update" in body


def test_detail_page_history_labels_a_legacy_row_predating_kind_as_deploy(
    client, fake_client, dashboard_state
):
    """dashboard_state's fixture row has no kind set -- it must read as a
    plain Deploy, not blank or an error.
    """
    body = client.get('/app/myapp').get_data(as_text=True)

    assert "Deploy" in body
