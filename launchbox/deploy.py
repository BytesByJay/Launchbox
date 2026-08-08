"""Deployment orchestrator.

This is the component Chapter 3 of the dissertation designed and Chapter 4
records as missing: a single place that owns step ordering and error handling
for a deployment, so that a failure at any stage leaves the previously running
version serving traffic.

The ordering guarantee is simple and worth stating plainly. Nothing that
destroys the running version happens until the replacement has been created,
started and verified healthy. If any earlier step raises, the running container
and its route are untouched.
"""

import json
import os
import time
from typing import Any, Dict, List, Optional

from launchbox import builder, router, runner
from launchbox.config import APPS_DIR, validate_app_name
from launchbox.config_parser import LaunchboxConfig
from launchbox.database_manager import DatabaseManager
from launchbox.logger import setup_logger, DeploymentError, LaunchboxError
from launchbox.state import StateStore

logger = setup_logger("deploy")

# How long to let Traefik notice a rewritten route file before the container
# the old route pointed at is destroyed.
#
# Traefik's file provider does not apply a change the instant it is written:
# it debounces reloads by `providersThrottleDuration`, which defaults to 2s.
# Removing the previous container immediately after write_route therefore
# leaves a window in which the live routing table still names a container that
# no longer exists, and every request in that window is a 502. Waiting longer
# than the throttle window closes it. Fail-closed behaviour never depended on
# this -- the wait is what preserves the zero-downtime property.
#
# Override with LAUNCHBOX_ROUTE_PROPAGATION (seconds) for a differently tuned
# Traefik, or with 0 to disable the wait entirely.
ROUTE_PROPAGATION_SECONDS = 3.0


def _route_propagation_seconds() -> float:
    raw = os.environ.get("LAUNCHBOX_ROUTE_PROPAGATION")
    if raw is None:
        return ROUTE_PROPAGATION_SECONDS
    try:
        return max(0.0, float(raw))
    except (TypeError, ValueError):
        logger.warning(
            f"Ignoring invalid LAUNCHBOX_ROUTE_PROPAGATION={raw!r}; "
            f"using {ROUTE_PROPAGATION_SECONDS}s"
        )
        return ROUTE_PROPAGATION_SECONDS


def _await_route_propagation() -> None:
    """Block until a freshly written route can be assumed live."""
    delay = _route_propagation_seconds()
    if delay <= 0:
        return
    logger.info(f"Waiting {delay:.1f}s for Traefik to pick up the new route")
    time.sleep(delay)


def _short(commit_sha: Optional[str]) -> str:
    return commit_sha[:7] if commit_sha else "latest"


def _find_previous_container(app_name: str, store: StateStore) -> Optional[str]:
    """The container currently serving this app, if any.

    Checks the state store first. If the store has no usable record -- for
    example because the state database was lost or reset while a container
    was still running -- falls back to a label lookup, since every container
    this pipeline creates carries a ``launchbox.app`` label. That is what
    stops a stale/missing state row from making a redeploy of the same SHA
    force-remove the container currently serving traffic before anything new
    exists to replace it.

    Falls back further to the pre-swap naming scheme so the first deployment
    after this change migrates without manual intervention.
    """
    current = store.current_container(app_name)
    if current and runner.container_exists(current):
        return current

    labelled = runner.find_app_containers(app_name)

    # Two passes, deliberately. find_app_containers lists ALL containers
    # (docker ps -a), newest-created first -- and a stale, stopped container
    # left behind by a deploy that died between create and cleanup (see the
    # comment in _promote) is often newer than the container actually
    # serving traffic. A stale stopped container must never outrank the one
    # that is running, so the running check is tried across every candidate
    # before the mere-existence check is tried against any of them.
    for name in labelled:
        if runner.container_is_running(name):
            logger.info(
                f"Recovered live container '{name}' for {app_name} via label "
                "lookup (state store had no usable record)"
            )
            return name

    for name in labelled:
        if runner.container_exists(name):
            logger.info(
                f"Recovered stopped container '{name}' for {app_name} via "
                "label lookup (state store had no usable record; nothing "
                "labelled for this app is currently running)"
            )
            return name

    if runner.container_exists(app_name):
        logger.warning(
            f"Adopting container named '{app_name}' by name; it carries no "
            "launchbox.app label, so it was likely created by a pipeline "
            "version predating the label-based lookup"
        )
        return app_name

    return current


def _database_environment(
    app_name: str, source_dir: Optional[str], config: LaunchboxConfig
) -> Dict[str, str]:
    """Provision this application's database and return its credentials.

    Split out of _collect_environment so a rollback can re-run *only* this
    part. Database credentials must never be replayed from a stored
    environment: provisioning is idempotent and the live credentials are
    whatever the database container has now, which is not necessarily what a
    past deployment recorded.
    """
    if not config.is_database_enabled():
        return {}

    manager = DatabaseManager()
    connection_info = manager.create_database_for_app(
        app_name, source_dir, config=config
    )
    if not connection_info:
        return {}

    logger.info(f"Database provisioned for {app_name}")
    return dict(connection_info)


def _stringify_env(env_vars: Dict[str, Any]) -> Dict[str, str]:
    return {str(k): str(v) for k, v in env_vars.items()}


def _collect_environment(
    app_name: str, source_dir: str, config: LaunchboxConfig
) -> Dict[str, str]:
    env_vars = dict(config.get_environment_vars())
    env_vars.update(_database_environment(app_name, source_dir, config))
    return _stringify_env(env_vars)


def _record_failure_once(store: StateStore, deployment_id: int, error: Exception) -> None:
    """Record a failure only if nothing has been recorded for it yet.

    _promote records probe failures itself, with the failed container's logs
    attached. This must not overwrite that richer record -- but a
    DeploymentError raised anywhere else (a container that will not start, an
    unreachable network) would otherwise leave the row stuck at in_progress.
    """
    record = store.get_deployment(deployment_id)
    if record is not None and record["status"] == "in_progress":
        store.record_failure(deployment_id, str(error))


def _run_promotion(store, deployment_id, promote_callable):
    """Run a promotion, guaranteeing the deployment row reaches a terminal state.

    Both deploy() and rollback() promote a container through _promote, and both
    must record a failure whichever way that fails. Keeping the handling in one
    place is deliberate: rollback was added after deploy's handlers and silently
    did not inherit them.
    """
    try:
        return promote_callable()
    except DeploymentError as exc:
        _record_failure_once(store, deployment_id, exc)
        raise
    except LaunchboxError as exc:
        _record_failure_once(store, deployment_id, exc)
        raise DeploymentError(str(exc)) from exc
    except Exception as exc:
        _record_failure_once(store, deployment_id, exc)
        raise DeploymentError(str(exc)) from exc


def _reap_stray_containers(app_name: str, keep: str) -> List[str]:
    """Remove every container of this app except the one just promoted.

    Only the single container adopted as ``previous`` was ever cleaned up, so
    siblings accumulated: if write_route raises, the new container is left
    running and unrouted, and a later deploy can adopt that orphan as
    ``previous`` and remove it instead of the container actually serving --
    which then leaks forever. Reaping after a successful promotion is the
    point at which exactly one container is known to be correct.

    Runs after the route has been written, verified and given time to
    propagate, so nothing serving traffic is in the set.
    """
    reaped = []
    for name in runner.find_app_containers(app_name):
        if name == keep:
            continue
        if runner.stop_and_remove(name):
            logger.info(f"Reaped stray container for {app_name}: {name}")
            reaped.append(name)
    return reaped


def _promote(
    app_name: str,
    container_name: str,
    image_ref: str,
    config: LaunchboxConfig,
    env_vars: Dict[str, str],
    commit_sha: Optional[str],
    previous_container: Optional[str],
    store: StateStore,
    deployment_id: int,
    image_id: Optional[str] = None,
) -> str:
    """Create, verify, then route. Shared by deploy and rollback."""
    # A stale container under the same name would block creation; it can only
    # exist if a previous attempt died between creation and cleanup.
    if container_name != previous_container:
        runner.stop_and_remove(container_name)

    runner.create_container(
        container_name=container_name,
        image_ref=image_ref,
        port=config.get_port(),
        env_vars=env_vars,
        resource_limits=config.get_resource_limits(),
        health_check=config.get_health_check(),
        app_name=app_name,
        commit_sha=commit_sha,
    )

    healthy = runner.wait_for_health(container_name, config.get_health_check())

    if not healthy:
        logs = runner.container_logs(container_name)
        runner.stop_and_remove(container_name)
        store.record_failure(deployment_id, "health probe failed", logs)
        raise DeploymentError(
            f"{app_name}: health probe failed; previous version left running"
        )

    router.write_route(
        app_name,
        container_name,
        config.get_port(),
        https=config.is_https_enabled(),
        redirect_http=config.should_redirect_http(),
    )

    # The route file has been rewritten, but Traefik has not necessarily
    # reloaded it yet -- so the previously running container may still be the
    # one traffic is reaching. Destroying it now is what produced 502s.
    _await_route_propagation()

    if previous_container and previous_container != container_name:
        runner.stop_and_remove(previous_container)

    _reap_stray_containers(app_name, keep=container_name)

    store.record_success(deployment_id, container_name, image_ref, image_id=image_id)
    store.reset_restart_attempts(app_name)
    store.clear_failed(app_name)

    logger.info(f"{app_name} deployed: http://{app_name}.localhost")
    return container_name


def deploy(
    app_name: str,
    source_dir: Optional[str] = None,
    commit_sha: Optional[str] = None,
    store: Optional[StateStore] = None,
) -> str:
    """Build and deploy an application, returning the new container name.

    Args:
        app_name: the registered application.
        source_dir: what to build. Defaults to ``apps/<app_name>``. The git
            hook passes a temporary worktree holding the pushed commit.
        commit_sha: the commit being deployed; used for image and container
            naming and recorded in the deployment history.
        store: injectable state store, for testing.
    """
    validate_app_name(app_name)

    build_source = source_dir or os.path.join(APPS_DIR, app_name)
    owns_store = store is None
    deployment_id: Optional[int] = None

    try:
        store = store or StateStore()

        deployment_id = store.record_start(app_name, commit_sha)
        logger.info(f"Deployment {deployment_id} started for {app_name}")

        config = LaunchboxConfig(build_source)

        image_ref = builder.build(app_name, source_dir=build_source,
                                  tag=_short(commit_sha))
        env_vars = _collect_environment(app_name, build_source, config)

        # Persist what this deployment actually resolved to. A rollback
        # replays these rather than re-reading apps/<app>/, which for a
        # git-pushed application never exists -- reading it there yielded
        # built-in defaults (port 3000, no health check, no limits, no env).
        store.record_config(
            deployment_id,
            json.dumps(config.config, default=str, sort_keys=True),
            json.dumps(env_vars, sort_keys=True),
        )

        # Resolved now, while the tag still points at the image just built.
        resolved_image_id = runner.image_id(image_ref)

        previous = _find_previous_container(app_name, store)
        container_name = f"{app_name}-{_short(commit_sha)}"
        if container_name == previous:
            container_name = f"{container_name}-{int(time.time())}"

        return _run_promotion(store, deployment_id, lambda: _promote(
            app_name=app_name,
            container_name=container_name,
            image_ref=image_ref,
            config=config,
            env_vars=env_vars,
            commit_sha=commit_sha,
            previous_container=previous,
            store=store,
            deployment_id=deployment_id,
            image_id=resolved_image_id,
        ))

    except DeploymentError as exc:
        if deployment_id is not None:
            _record_failure_once(store, deployment_id, exc)
        raise
    except LaunchboxError as exc:
        if deployment_id is not None:
            _record_failure_once(store, deployment_id, exc)
        logger.error(f"Deployment {deployment_id} failed: {exc}")
        raise DeploymentError(str(exc)) from exc
    except Exception as exc:
        if deployment_id is not None:
            _record_failure_once(store, deployment_id, exc)
        logger.error(f"Deployment {deployment_id} failed unexpectedly: {exc}")
        raise DeploymentError(str(exc)) from exc
    finally:
        if owns_store and store is not None:
            store.close()


def _resolve_rollback_image(app_name: str, target: Dict[str, Any]) -> str:
    """The image reference a rollback should run, refusing ambiguous ones.

    The recorded immutable image ID wins. ``image_tag`` is only a fallback for
    rows written before IDs were recorded, and is refused outright when it is
    ``:latest`` -- builder.build re-points that tag on every build, so
    resolving it can land on a NEWER image than the one being rolled back to,
    i.e. exactly the build the operator is escaping, reported as a success.
    """
    recorded_id = target.get("image_id")
    image_tag = target.get("image_tag")

    if recorded_id:
        image_ref = recorded_id
    elif not image_tag:
        raise DeploymentError(
            f"{app_name}: deployment {target['id']} recorded no image at all; "
            "it cannot be rolled back to without rebuilding"
        )
    elif image_tag.endswith(":latest"):
        raise DeploymentError(
            f"{app_name}: deployment {target['id']} recorded only the moving "
            f"tag {image_tag} and no immutable image ID. That tag is "
            "re-pointed by every later build, so it no longer identifies the "
            "image this deployment ran. Roll back to a commit-tagged "
            "deployment, or redeploy the desired commit."
        )
    else:
        image_ref = image_tag

    if not runner.image_exists(image_ref):
        raise DeploymentError(
            f"{app_name}: image {image_ref} is no longer present; "
            "it cannot be rolled back to without rebuilding"
        )
    return image_ref


def _replayed_config(app_name: str, target: Dict[str, Any]) -> LaunchboxConfig:
    """Reconstruct the configuration a past deployment ran with.

    Refusing a row that has none is deliberate. Falling back to reading
    ``apps/<app>/`` silently produced built-in defaults for every git-pushed
    application -- no health check, no resource limits, no environment, port
    3000 -- and _promote would then remove the healthy container and publish a
    route to something not serving.
    """
    raw = target.get("config_json")
    if not raw:
        raise DeploymentError(
            f"{app_name}: deployment {target['id']} predates deployment "
            "configuration capture, so the configuration it ran with is "
            "unknown and cannot be replayed. Rolling back with default "
            "configuration would take the application down. Redeploy the "
            "desired commit instead."
        )
    try:
        mapping = json.loads(raw)
    except ValueError as exc:
        raise DeploymentError(
            f"{app_name}: stored configuration for deployment "
            f"{target['id']} is unreadable: {exc}"
        ) from exc
    if not isinstance(mapping, dict):
        raise DeploymentError(
            f"{app_name}: stored configuration for deployment "
            f"{target['id']} is not a configuration mapping"
        )
    return LaunchboxConfig.from_mapping(mapping)


def _replayed_environment(
    app_name: str, config: LaunchboxConfig, target: Dict[str, Any]
) -> Dict[str, str]:
    """Stored environment, with database credentials re-provisioned live.

    Database credentials are never replayed from the row: provisioning is
    idempotent and must run so the database container exists and is started,
    and the credentials it yields now are the authoritative ones. Everything
    else the deployment ran with is merged underneath.
    """
    raw = target.get("env_json")
    try:
        stored = json.loads(raw) if raw else {}
    except ValueError:
        logger.warning(
            f"{app_name}: stored environment for deployment {target['id']} is "
            "unreadable; continuing with configured environment only"
        )
        stored = {}
    if not isinstance(stored, dict):
        stored = {}

    env_vars = dict(stored)
    env_vars.update(_database_environment(app_name, None, config))
    return _stringify_env(env_vars)


def remove_app(
    app_name: str,
    store: Optional[StateStore] = None,
    dynamic_dir: str = router.DYNAMIC_DIR,
    remove_database: bool = False,
) -> Dict[str, Any]:
    """Tear an application down completely.

    The order is the point. The route file goes first, so no traffic is sent
    to something about to disappear; then the containers; then this app's
    images; then the state row. Doing it in any other order leaves Traefik
    with a router aimed at a deleted backend, or leaves the application
    listed by ``launchbox list`` with nothing behind it.

    Databases are left alone unless explicitly asked for: removing an
    application should not silently destroy its data.
    """
    validate_app_name(app_name)

    owns_store = store is None
    store = store or StateStore()
    summary: Dict[str, Any] = {
        "app_name": app_name,
        "route_removed": False,
        "containers": [],
        "images": [],
        "database_removed": False,
    }

    try:
        summary["route_removed"] = router.remove_route(
            app_name, dynamic_dir=dynamic_dir
        )

        candidates = []
        for name in (
            store.current_container(app_name),
            *runner.find_app_containers(app_name),
            app_name,  # the pre-swap naming scheme
        ):
            if name and name not in candidates:
                candidates.append(name)

        for name in candidates:
            if runner.stop_and_remove(name):
                summary["containers"].append(name)

        for image_ref in runner.list_images(builder.image_name(app_name)):
            if runner.remove_image(image_ref):
                summary["images"].append(image_ref)

        if remove_database:
            summary["database_removed"] = DatabaseManager(
            ).remove_database_for_app(app_name)
            store.delete_database(app_name)

        store.forget_app(app_name)
        logger.info(
            f"Removed {app_name}: {len(summary['containers'])} container(s), "
            f"{len(summary['images'])} image(s)"
        )
        return summary

    finally:
        if owns_store:
            store.close()


RESERVED_ENV_KEYS = frozenset({
    'DATABASE_URL', 'DB_HOST', 'DB_PORT', 'DB_NAME', 'DB_USER',
    'DB_PASSWORD', 'DB_TYPE',
})


def update_env(
    app_name: str,
    set_vars: Optional[Dict[str, str]] = None,
    unset_vars: Optional[List[str]] = None,
    store: Optional[StateStore] = None,
) -> str:
    """Redeploy the running image with updated environment variables.

    No rebuild: the current deployment's own image is reused, so this is a
    live patch to the running container rather than a change to the
    application's source. The next real deploy -- a git push, or
    `launchbox deploy` -- reads launchbox.yaml/.env fresh and supersedes
    whatever was set here. Nothing is written to disk.

    Goes through the same health-gated promotion path as a forward deploy and
    as rollback, so an update whose probe fails leaves the current version
    serving.

    Database connection variables are provisioned automatically and
    re-resolved live on every call; they cannot be set or unset through this
    path, so a stray edit here can never desynchronise a container from the
    database it is actually pointed at.
    """
    validate_app_name(app_name)

    requested_keys = set((set_vars or {}).keys()) | set(unset_vars or [])
    reserved_requested = requested_keys & RESERVED_ENV_KEYS
    if reserved_requested:
        raise ValueError(
            f"{app_name}: {', '.join(sorted(reserved_requested))} "
            "are database connection variables managed automatically and "
            "cannot be set or unset here."
        )

    owns_store = store is None
    store = store or StateStore()

    try:
        app_row = store.get_app(app_name)
        current_id = app_row.get('current_deployment') if app_row else None
        if not current_id:
            raise DeploymentError(f"{app_name}: no current deployment to update")

        target = store.get_deployment(current_id)
        if target is None:
            raise DeploymentError(f"{app_name}: current deployment record is missing")

        image_ref = _resolve_rollback_image(app_name, target)
        config = _replayed_config(app_name, target)
        env_vars = _replayed_environment(app_name, config, target)

        for key in (unset_vars or []):
            env_vars.pop(key, None)
        env_vars.update(_stringify_env(set_vars or {}))

        previous = _find_previous_container(app_name, store)
        commit_sha = target.get('commit_sha')
        container_name = f"{app_name}-{_short(commit_sha)}"
        if runner.container_exists(container_name) or container_name == previous:
            container_name = f"{container_name}-{int(time.time())}"

        deployment_id = store.record_start(
            app_name,
            commit_sha,
            config_json=json.dumps(config.config, default=str, sort_keys=True),
            env_json=json.dumps(env_vars, sort_keys=True),
        )
        logger.info(
            f"Updating environment for {app_name} (deployment {deployment_id})"
        )

        return _run_promotion(store, deployment_id, lambda: _promote(
            app_name=app_name,
            container_name=container_name,
            image_ref=image_ref,
            config=config,
            env_vars=env_vars,
            commit_sha=commit_sha,
            previous_container=previous,
            store=store,
            deployment_id=deployment_id,
            image_id=target.get('image_id'),
        ))
    finally:
        if owns_store:
            store.close()


def rollback(
    app_name: str,
    commit_sha: Optional[str] = None,
    store: Optional[StateStore] = None,
) -> str:
    """Redeploy a previous successful version without rebuilding it.

    Args:
        app_name: the application to roll back.
        commit_sha: an explicit target. Defaults to the most recent successful
            deployment that is not the one currently running.

    The promotion path is identical to a forward deployment, so a rollback that
    fails its own health probe leaves the current version serving.
    """
    validate_app_name(app_name)

    owns_store = store is None
    store = store or StateStore()

    try:
        if commit_sha:
            target = store.find_successful_deployment_by_sha(app_name, commit_sha)
            if target is None:
                raise DeploymentError(
                    f"{app_name}: no successful deployment matching {commit_sha}"
                )
        else:
            target = store.last_successful_deployment(app_name)
            if target is None:
                raise DeploymentError(
                    f"{app_name}: no previous successful deployment to roll back to"
                )

        image_ref = _resolve_rollback_image(app_name, target)
        config = _replayed_config(app_name, target)
        env_vars = _replayed_environment(app_name, config, target)

        previous = _find_previous_container(app_name, store)
        target_sha = target["commit_sha"]
        container_name = f"{app_name}-{_short(target_sha)}"
        if runner.container_exists(container_name) or container_name == previous:
            container_name = f"{container_name}-{int(time.time())}"

        deployment_id = store.record_start(
            app_name,
            target_sha,
            config_json=json.dumps(config.config, default=str, sort_keys=True),
            env_json=json.dumps(env_vars, sort_keys=True),
        )
        logger.info(
            f"Rolling {app_name} back to {_short(target_sha)} "
            f"(deployment {deployment_id})"
        )

        return _run_promotion(store, deployment_id, lambda: _promote(
            app_name=app_name,
            container_name=container_name,
            image_ref=image_ref,
            config=config,
            env_vars=env_vars,
            commit_sha=target_sha,
            previous_container=previous,
            store=store,
            deployment_id=deployment_id,
            image_id=target.get("image_id"),
        ))

    finally:
        if owns_store:
            store.close()
