import shutil
import subprocess

import pytest


@pytest.fixture(autouse=True)
def _no_route_propagation_wait(monkeypatch):
    """Neutralise deploy's Traefik route-propagation wait for the whole suite.

    deploy.ROUTE_PROPAGATION_SECONDS defaults to 3s of real sleeping after
    every route write. Setting the documented override to 0 keeps the suite
    fast without patching time.sleep globally; tests that care about the wait
    assert on the call ordering or set their own value.
    """
    monkeypatch.setenv("LAUNCHBOX_ROUTE_PROPAGATION", "0")


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
