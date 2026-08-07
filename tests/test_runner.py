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


# ------------------------------------------------------------- find_app_containers


def test_find_app_containers_parses_newline_separated_names(mocker):
    mocker.patch(
        "launchbox.runner.subprocess.run",
        return_value=_completed(stdout="myapp-3333333\nmyapp-2222222\n\n"),
    )
    assert runner.find_app_containers("myapp") == ["myapp-3333333", "myapp-2222222"]


def test_find_app_containers_returns_empty_list_on_docker_failure(mocker):
    mocker.patch(
        "launchbox.runner.subprocess.run",
        return_value=_completed(returncode=1, stderr="Cannot connect to daemon"),
    )
    assert runner.find_app_containers("myapp") == []


def test_find_app_containers_filters_by_app_label(mocker):
    run = mocker.patch(
        "launchbox.runner.subprocess.run", return_value=_completed(stdout="")
    )
    runner.find_app_containers("myapp")
    cmd = run.call_args[0][0]
    assert "label=launchbox.app=myapp" in cmd


def test_find_app_containers_returns_empty_list_on_empty_stdout(mocker):
    mocker.patch(
        "launchbox.runner.subprocess.run", return_value=_completed(stdout="")
    )
    assert runner.find_app_containers("myapp") == []


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


def test_create_container_rejects_traversing_app_name(mocker):
    run = mocker.patch("launchbox.runner.subprocess.run")
    mocker.patch("launchbox.runner.ensure_network")

    with pytest.raises(ValueError):
        runner.create_container(
            container_name="evil-abc1234",
            image_ref="launchbox-evil:abc1234",
            port=3000,
            env_vars={},
            resource_limits={},
            health_check=None,
            app_name="../../etc/passwd",
        )
    run.assert_not_called()


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


# ----------------------------------------------------------------- image_id


def test_image_id_returns_the_immutable_id(mocker):
    run = mocker.patch(
        "launchbox.runner.subprocess.run",
        return_value=_completed(stdout="sha256:deadbeefcafe\n"),
    )
    assert runner.image_id("launchbox-myapp:latest") == "sha256:deadbeefcafe"

    cmd = run.call_args[0][0]
    assert cmd[:4] == ["docker", "image", "inspect", "-f"]
    assert "{{.Id}}" in cmd
    assert "launchbox-myapp:latest" in cmd


def test_image_id_returns_none_when_the_image_is_absent(mocker):
    mocker.patch(
        "launchbox.runner.subprocess.run",
        return_value=_completed(returncode=1, stderr="No such image"),
    )
    assert runner.image_id("launchbox-myapp:gone") is None


def test_image_id_returns_none_on_empty_output(mocker):
    mocker.patch(
        "launchbox.runner.subprocess.run", return_value=_completed(stdout="\n")
    )
    assert runner.image_id("launchbox-myapp:latest") is None


# ------------------------------------------------- find_app_containers filters


def test_find_app_containers_excludes_the_database_container(mocker):
    """DatabaseManager labels an application's database container with the
    same launchbox.app value. Callers of this function stop and remove what it
    returns, so without the service filter a redeploy or a reap would destroy
    the application's database.
    """
    run = mocker.patch(
        "launchbox.runner.subprocess.run", return_value=_completed(stdout="")
    )
    runner.find_app_containers("myapp")

    cmd = run.call_args[0][0]
    assert "label=launchbox.app=myapp" in cmd
    assert "label=launchbox.service=app" in cmd


# ------------------------------------------------------ image listing/removal


def test_list_images_returns_every_tagged_reference(mocker):
    mocker.patch(
        "launchbox.runner.subprocess.run",
        return_value=_completed(
            stdout="launchbox-myapp:abc1234\nlaunchbox-myapp:latest\n"
        ),
    )
    assert runner.list_images("launchbox-myapp") == [
        "launchbox-myapp:abc1234",
        "launchbox-myapp:latest",
    ]


def test_list_images_skips_untagged_entries(mocker):
    mocker.patch(
        "launchbox.runner.subprocess.run",
        return_value=_completed(
            stdout="launchbox-myapp:abc1234\nlaunchbox-myapp:<none>\n"
        ),
    )
    assert runner.list_images("launchbox-myapp") == ["launchbox-myapp:abc1234"]


def test_list_images_returns_empty_list_on_docker_failure(mocker):
    mocker.patch(
        "launchbox.runner.subprocess.run",
        return_value=_completed(returncode=1, stderr="Cannot connect"),
    )
    assert runner.list_images("launchbox-myapp") == []


def test_remove_image_force_removes_and_reports_success(mocker):
    run = mocker.patch(
        "launchbox.runner.subprocess.run", return_value=_completed()
    )
    assert runner.remove_image("launchbox-myapp:abc1234") is True
    assert run.call_args[0][0] == [
        "docker", "rmi", "-f", "launchbox-myapp:abc1234"
    ]


def test_remove_image_reports_failure_without_raising(mocker):
    mocker.patch(
        "launchbox.runner.subprocess.run",
        return_value=_completed(returncode=1, stderr="image is in use"),
    )
    assert runner.remove_image("launchbox-myapp:abc1234") is False
