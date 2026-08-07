"""Docker image construction.

Images are tagged by commit so that a previous build survives a later one and
can be redeployed without rebuilding. A moving ``:latest`` tag always points at
the most recent successful build.
"""

import os
import subprocess
import sys
from typing import Optional

from launchbox.config import APPS_DIR, validate_app_name
from launchbox.config_parser import LaunchboxConfig
from launchbox.logger import setup_logger, BuildError

logger = setup_logger("builder")


def image_name(app_name: str) -> str:
    return f"launchbox-{app_name}"


def build(
    app_name: str,
    source_dir: Optional[str] = None,
    tag: Optional[str] = None,
) -> str:
    """Build the application image and return its full reference.

    Args:
        app_name: application whose image is being built.
        source_dir: directory to build from. Defaults to ``apps/<app_name>``.
            The deployment pipeline passes a temporary worktree containing the
            exact commit that was pushed.
        tag: image tag, normally a short commit SHA. Defaults to ``latest``.

    Returns:
        The primary image reference, ``launchbox-<app>:<tag>``.
    """
    validate_app_name(app_name)

    build_source = source_dir or os.path.join(APPS_DIR, app_name)

    if not os.path.isdir(build_source):
        raise BuildError(f"Application source directory not found: {build_source}")

    config = LaunchboxConfig(build_source)
    dockerfile = config.get_dockerfile()
    build_context = config.get_build_context()

    dockerfile_path = os.path.join(build_source, dockerfile)
    if not os.path.exists(dockerfile_path):
        raise BuildError(f"Dockerfile not found: {dockerfile_path}")

    base = image_name(app_name)
    effective_tag = tag or "latest"
    primary_ref = f"{base}:{effective_tag}"
    build_path = os.path.normpath(os.path.join(build_source, build_context))

    build_cmd = ["docker", "build", "-t", primary_ref]
    if effective_tag != "latest":
        build_cmd.extend(["-t", f"{base}:latest"])
    build_cmd.extend(["-f", dockerfile_path, build_path])

    logger.info(f"Building image: {primary_ref}")
    logger.info(f"Build context: {build_path}")
    logger.debug(f"Running command: {' '.join(build_cmd)}")

    result = subprocess.run(build_cmd, capture_output=True, text=True, check=False)

    if result.returncode != 0:
        logger.error(f"Docker build failed for {app_name}")
        logger.error(f"stdout: {result.stdout}")
        logger.error(f"stderr: {result.stderr}")
        raise BuildError(f"Docker build failed: {result.stderr}")

    logger.info(f"Successfully built image: {primary_ref}")
    return primary_ref


if __name__ == "__main__":
    if len(sys.argv) < 2:
        logger.error("Usage: python3 -m launchbox.builder <app_name>")
        sys.exit(1)

    try:
        build(sys.argv[1])
        sys.exit(0)
    except (BuildError, ValueError) as exc:
        logger.error(f"Build failed: {exc}")
        sys.exit(1)
