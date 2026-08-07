"""Health supervision.

Docker reports whether a container is healthy; on its own, nothing acts on that
report. This module closes that loop: an unhealthy container is restarted, and
one that keeps failing is marked failed rather than restarted forever, so a
genuine crash loop cannot consume the host.
"""

import os
import threading
import time
from typing import Any, Callable, Dict, List, Optional

from launchbox import runner
from launchbox.logger import setup_logger
from launchbox.state import StateStore

logger = setup_logger("supervisor")

POLL_INTERVAL = float(os.environ.get("LAUNCHBOX_SUPERVISOR_INTERVAL", 30))
MAX_RESTART_ATTEMPTS = int(os.environ.get("LAUNCHBOX_MAX_RESTARTS", 3))


def supervise_once(
    store: StateStore,
    health_fn: Callable[[str], str] = runner.docker_health_status,
    restart_fn: Callable[[str], bool] = runner.restart_container,
    max_attempts: int = MAX_RESTART_ATTEMPTS,
) -> List[Dict[str, Any]]:
    """Evaluate every deployed application once.

    Returns the actions taken, which makes the decision logic testable without
    a Docker daemon and gives the dashboard something to display.
    """
    actions: List[Dict[str, Any]] = []

    for app in store.list_apps():
        app_name = app["app_name"]
        container = app["current_container"]

        if not container:
            continue

        if store.is_marked_failed(app_name):
            actions.append(
                {"app": app_name, "action": "skipped", "container": container}
            )
            continue

        status = health_fn(container)

        if status in ("healthy", "none"):
            # "none" means the container declares no probe. Absence of a probe
            # is not evidence of failure, so leave it alone.
            store.set_health(app_name, "healthy")
            store.reset_restart_attempts(app_name)
            actions.append(
                {"app": app_name, "action": "healthy", "container": container}
            )
            continue

        if status == "starting":
            store.set_health(app_name, "starting")
            actions.append(
                {"app": app_name, "action": "starting", "container": container}
            )
            continue

        if status == "missing":
            store.set_health(app_name, "unknown")
            logger.warning(f"{app_name}: container {container} is gone")
            actions.append(
                {"app": app_name, "action": "missing", "container": container}
            )
            continue

        # status == "unhealthy"
        store.set_health(app_name, "unhealthy")
        attempts = store.restart_attempts(app_name)

        if attempts >= max_attempts:
            store.mark_failed(app_name)
            logger.error(
                f"{app_name}: still unhealthy after {attempts} restarts; "
                "marking failed and giving up"
            )
            actions.append(
                {"app": app_name, "action": "marked_failed", "container": container}
            )
            continue

        logger.warning(
            f"{app_name}: unhealthy, restarting "
            f"(attempt {attempts + 1} of {max_attempts})"
        )
        ok = restart_fn(container)
        store.increment_restart_attempts(app_name)
        actions.append({
            "app": app_name,
            "action": "restarted" if ok else "restart_failed",
            "container": container,
        })

    return actions


def run_forever(interval: Optional[float] = None) -> None:
    """Poll indefinitely. Used by ``python3 -m launchbox supervisor``."""
    poll = interval or POLL_INTERVAL
    logger.info(f"Supervisor started, polling every {poll:.0f}s")

    store = StateStore()
    try:
        while True:
            try:
                supervise_once(store)
            except Exception as exc:  # never let one bad pass kill the loop
                logger.error(f"Supervisor pass failed: {exc}")
            time.sleep(poll)
    finally:
        store.close()


def start_background_thread(interval: Optional[float] = None) -> threading.Thread:
    """Run the supervisor alongside another process, such as the dashboard."""
    thread = threading.Thread(
        target=run_forever,
        kwargs={"interval": interval},
        name="launchbox-supervisor",
        daemon=True,
    )
    thread.start()
    return thread
