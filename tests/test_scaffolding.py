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
