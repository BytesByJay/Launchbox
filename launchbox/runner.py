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
from typing import Any, Dict, List, Optional

from launchbox.config import validate_app_name
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


def find_app_containers(app_name: str) -> List[str]:
    """Names of all containers Launchbox created for an application.

    Used to recover the live container when the state store has no record of
    it -- for example after the state database is lost. Newest first.
    """
    result = _docker([
        "ps", "-a",
        "--filter", f"label=launchbox.app={app_name}",
        # Both filters are required. DatabaseManager tags an application's
        # database container with the same launchbox.app label, so filtering
        # on that alone returns the database as a candidate -- and callers of
        # this function stop and remove what it returns.
        "--filter", "label=launchbox.service=app",
        "--format", "{{.Names}}",
    ])
    if result.returncode != 0:
        return []
    return [line.strip() for line in result.stdout.splitlines() if line.strip()]


def container_is_running(container_name: str) -> bool:
    result = _docker(
        ["container", "inspect", "-f", "{{.State.Running}}", container_name]
    )
    return result.returncode == 0 and result.stdout.strip() == "true"


def image_exists(image_ref: str) -> bool:
    return _docker(["image", "inspect", image_ref]).returncode == 0


def image_id(image_ref: str) -> Optional[str]:
    """The immutable ID (``sha256:...``) an image reference resolves to now.

    Recorded at deploy time so a rollback can address the exact image that
    deployment ran. A tag is not sufficient: ``launchbox-<app>:latest`` is
    re-pointed by every subsequent build, so resolving it later can return a
    newer image than the one being rolled back to.
    """
    result = _docker(["image", "inspect", "-f", "{{.Id}}", image_ref])
    if result.returncode != 0:
        return None
    return result.stdout.strip() or None


def list_images(repository: str) -> List[str]:
    """Every ``repo:tag`` reference under one image repository."""
    result = _docker([
        "images", "--format", "{{.Repository}}:{{.Tag}}", repository,
    ])
    if result.returncode != 0:
        return []
    return [
        line.strip()
        for line in result.stdout.splitlines()
        if line.strip() and not line.strip().endswith(":<none>")
    ]


def remove_image(image_ref: str) -> bool:
    """Force-remove an image. Returns True if Docker reported success."""
    result = _docker(["rmi", "-f", image_ref])
    if result.returncode != 0:
        logger.warning(f"Failed to remove image {image_ref}: {result.stderr.strip()}")
        return False
    logger.info(f"Removed image: {image_ref}")
    return True


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
    validate_app_name(app_name)
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
