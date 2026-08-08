from flask import Flask, render_template, request, jsonify, flash, redirect, url_for
import docker
import json
import subprocess
import os
from pathlib import Path
from datetime import datetime
from launchbox.config import APPS_DIR, REPOS_DIR
from launchbox.config_parser import LaunchboxConfig
from launchbox.deploy import (
    deploy, remove_app, rollback, update_env, RESERVED_ENV_KEYS,
    configure_database, detach_database, DATABASE_ENGINES,
)
from launchbox.init import init as init_app
from launchbox.ssl_manager import SSLManager
from launchbox.logger import setup_logger, LaunchboxError
from launchbox.state import StateStore

# The two injected variables that contain the password. Everything else
# Launchbox injects (host, port, database name, user, engine) is not a
# secret and is more useful on the page than hidden behind a click.
SECRET_DB_KEYS = frozenset({'DB_PASSWORD', 'DATABASE_URL'})

logger = setup_logger("dashboard")

app = Flask(__name__)
app.secret_key = 'launchbox-dashboard-key'


def resolve_container_name(app_name, store=None):
    """The container currently serving an application.

    Deployments name containers ``<app>-<sha7>``, not ``<app>``, so looking a
    container up by the bare application name finds nothing: every app read as
    "not deployed" and stop/start/logs all 404'd. The state store is the record
    of which container is live; the bare name remains as a fallback for
    containers created before the naming scheme changed.
    """
    current = None
    try:
        if store is not None:
            current = store.current_container(app_name)
        else:
            with StateStore() as own_store:
                current = own_store.current_container(app_name)
    except Exception as e:
        logger.warning(f"Failed to read state for {app_name}: {e}")

    return current or app_name


def get_docker_client():
    """Get Docker client"""
    try:
        return docker.from_env()
    except Exception as e:
        logger.error(f"Failed to connect to Docker: {e}")
        return None

def _health_label(container):
    """A human status distinguishing Docker's HEALTHCHECK from run state.

    ``container.status`` alone only says running/exited/created -- it cannot
    tell "running, health probe passing" from "running, health probe
    failing", which is exactly the distinction the health-gated deploy and
    the supervisor both act on. Only trusted while the container is
    actually running: a stopped container's last-known Health.Status is
    stale and must not override the fact that it is not running.
    """
    status = container.status
    if status != 'running':
        return status

    health = container.attrs.get('State', {}).get('Health')
    if not health:
        return 'running'

    probe_status = health.get('Status')
    if probe_status == 'healthy':
        return 'healthy'
    if probe_status == 'unhealthy':
        return 'unhealthy'
    if probe_status == 'starting':
        return 'starting'
    return 'running'


def _base_app_info(name, path=None):
    return {
        'name': name,
        'path': path,
        'has_local_dir': path is not None,
        'has_dockerfile': False,
        'has_config': False,
        'status': 'unknown',
        'health_status': 'unknown',
        'container_id': None,
        'created': None,
        'port': 3000,
        'url': f"http://{name}.localhost",
        'health_state': None,
        'marked_failed': False,
        'restart_attempts': 0,
        'has_repo': os.path.isdir(os.path.join(REPOS_DIR, f"{name}.git")),
        'last_deployed_at': None,
        'last_commit': None,
    }


def _apply_config_from_mapping(app_info, mapping):
    """Fill in port/HTTPS from an already-parsed launchbox.yaml mapping.

    Used both for apps with a local launchbox.yaml and for git-push-only
    apps, whose resolved configuration was captured on their deployment row
    instead (see LaunchboxConfig.from_mapping).
    """
    app_section = (mapping or {}).get('app', {}) or {}
    https_section = (mapping or {}).get('https', {}) or {}
    if app_section.get('port'):
        app_info['port'] = app_section['port']
    if https_section.get('enabled'):
        app_info['url'] = f"https://{app_info['name']}.localhost"


def _apply_state_row(app_info, state_row, store=None):
    if not state_row:
        return
    app_info['health_state'] = state_row.get('health_state')
    app_info['marked_failed'] = bool(state_row.get('marked_failed'))
    app_info['restart_attempts'] = int(state_row.get('restart_attempts') or 0)

    # When the app last actually went live, and on what commit. Reading it
    # from the current deployment rather than the container's created time
    # means it survives a container being restarted by hand.
    current_deployment = state_row.get('current_deployment')
    if store is not None and current_deployment:
        try:
            deployment = store.get_deployment(current_deployment)
            if deployment:
                app_info['last_deployed_at'] = deployment.get('started_at')
                app_info['last_commit'] = deployment.get('commit_sha')
        except Exception as e:
            logger.warning(
                f"Failed to read last deployment for {app_info['name']}: {e}"
            )


def _apply_container_info(app_info, store):
    client = get_docker_client()
    if not client:
        return
    try:
        container = client.containers.get(
            resolve_container_name(app_info['name'], store=store)
        )
        app_info['status'] = container.status
        app_info['health_status'] = _health_label(container)
        app_info['container_id'] = container.id[:12]
        app_info['created'] = container.attrs['Created']
    except docker.errors.NotFound:
        app_info['status'] = 'not deployed'
        app_info['health_status'] = 'not deployed'
    except Exception as e:
        logger.warning(f"Failed to get container info for {app_info['name']}: {e}")


def get_app_list():
    """Every known application: on-disk (apps/<name>/) union deployed-via-git-push.

    An application built and deployed purely by `git push` never gets a
    apps/<name> directory on the Launchbox host -- the hook builds from a
    temporary worktree -- so listing only apps/ left every such application
    invisible. The state store's `apps` table is the other half of the
    picture: every application that has ever been registered or deployed has
    a row there, directory or not.
    """
    by_name = {}

    apps_path = Path(APPS_DIR)
    if apps_path.exists():
        for app_dir in apps_path.iterdir():
            if not app_dir.is_dir():
                continue
            info = _base_app_info(app_dir.name, path=str(app_dir))
            info['has_dockerfile'] = (app_dir / 'Dockerfile').exists()
            info['has_config'] = (app_dir / 'launchbox.yaml').exists()
            if info['has_config']:
                try:
                    config = LaunchboxConfig(str(app_dir))
                    _apply_config_from_mapping(info, config.config)
                except Exception as e:
                    logger.warning(f"Failed to load config for {app_dir.name}: {e}")
            by_name[app_dir.name] = info

    try:
        store = StateStore()
    except Exception as e:
        logger.warning(f"Failed to open state store: {e}")
        store = None

    try:
        if store is not None:
            for state_row in store.list_apps():
                name = state_row['app_name']
                if name not in by_name:
                    info = _base_app_info(name, path=None)
                    # No local directory to read a config from -- the
                    # closest thing to it is what the last deployment
                    # actually ran with.
                    current_deployment = state_row.get('current_deployment')
                    if current_deployment:
                        try:
                            deployment = store.get_deployment(current_deployment)
                            raw_config = (deployment or {}).get('config_json')
                            if raw_config:
                                _apply_config_from_mapping(info, json.loads(raw_config))
                        except Exception as e:
                            logger.warning(
                                f"Failed to load recorded config for {name}: {e}"
                            )
                    by_name[name] = info

                _apply_state_row(by_name[name], state_row, store=store)

        for info in by_name.values():
            _apply_container_info(info, store)
    finally:
        if store is not None:
            store.close()

    return sorted(by_name.values(), key=lambda x: x['name'])

@app.route('/')
def dashboard():
    """Main dashboard"""
    apps = get_app_list()

    # Get Docker system info
    client = get_docker_client()
    docker_info = None
    if client:
        try:
            docker_info = client.info()
        except Exception as e:
            logger.warning(f"Failed to get Docker info: {e}")

    recent_activity = []
    try:
        with StateStore() as store:
            recent_activity = store.list_recent_deployments(limit=12)
    except Exception as e:
        logger.warning(f"Failed to load recent activity: {e}")

    return render_template('dashboard.html', apps=apps, docker_info=docker_info,
                          recent_activity=recent_activity)

@app.route('/api/apps')
def api_apps():
    """API endpoint to get apps"""
    return jsonify(get_app_list())

@app.route('/api/apps', methods=['POST'])
def api_register_app():
    """Register a new application: creates its bare repo and git hook.

    This is what `launchbox init <app>` does from the CLI. The application
    becomes visible immediately (see StateStore.register), showing "not
    deployed" until its first push.
    """
    payload = request.get_json(silent=True) or {}
    name = (payload.get('name') or '').strip()
    branch = (payload.get('branch') or 'main').strip()

    try:
        repo_path = init_app(name, default_branch=branch)
        return jsonify({
            'message': f'Registered {name}',
            'name': name,
            'repo_path': repo_path,
            'push_commands': [
                f'git remote add launchbox {repo_path}',
                f'git push launchbox {branch}',
            ],
        }), 201
    except ValueError as e:
        return jsonify({'error': str(e)}), 400
    except LaunchboxError as e:
        return jsonify({'error': str(e)}), 500
    except Exception as e:
        logger.error(f"Failed to register {name}: {e}")
        return jsonify({'error': str(e)}), 500

@app.route('/api/apps/<app_name>/deploy', methods=['POST'])
def api_deploy_app(app_name):
    """Deploy an application"""
    try:
        logger.info(f"Deploying app: {app_name}")

        # deploy() owns the whole pipeline, build included. Calling build()
        # first here built every image twice.
        container_name = deploy(app_name)

        return jsonify({
            'message': f'Successfully deployed {app_name}',
            'container': container_name,
        })
    
    except Exception as e:
        logger.error(f"Failed to deploy {app_name}: {e}")
        return jsonify({'error': str(e)}), 500

@app.route('/api/apps/<app_name>/rollback', methods=['POST'])
def api_rollback_app(app_name):
    """Roll back to a previous successful deployment.

    Redeploys a recorded image through the same health-gated promotion path
    as a forward deploy, so a rollback that fails its own probe leaves the
    currently running version serving. Never rebuilds.
    """
    try:
        payload = request.get_json(silent=True) or {}
        commit_sha = payload.get('commit_sha')

        container_name = rollback(app_name, commit_sha=commit_sha)

        return jsonify({
            'message': f'Rolled back {app_name}',
            'container': container_name,
        })

    except ValueError as e:
        return jsonify({'error': str(e)}), 400
    except LaunchboxError as e:
        return jsonify({'error': str(e)}), 500
    except Exception as e:
        logger.error(f"Failed to roll back {app_name}: {e}")
        return jsonify({'error': str(e)}), 500

@app.route('/api/apps/<app_name>/env', methods=['POST'])
def api_update_env(app_name):
    """Update environment variables on the running container, no rebuild.

    A live patch: redeploys the current image through the same health-gated
    path as rollback, so a change whose probe fails leaves the running
    version untouched. Nothing is written back to the application's source --
    the next real deploy supersedes this.
    """
    try:
        payload = request.get_json(silent=True) or {}
        set_vars = payload.get('set') or {}
        unset_vars = payload.get('unset') or []

        container_name = update_env(app_name, set_vars=set_vars,
                                    unset_vars=unset_vars)

        return jsonify({
            'message': f'Updated environment for {app_name}',
            'container': container_name,
        })

    except ValueError as e:
        return jsonify({'error': str(e)}), 400
    except LaunchboxError as e:
        return jsonify({'error': str(e)}), 500
    except Exception as e:
        logger.error(f"Failed to update environment for {app_name}: {e}")
        return jsonify({'error': str(e)}), 500

@app.route('/api/apps/<app_name>/database', methods=['POST'])
def api_configure_database(app_name):
    """Provision a database for the running application, no rebuild.

    Redeploys the current image through the same health-gated path as
    rollback, with the database section of its configuration overridden to
    the requested engine.
    """
    try:
        payload = request.get_json(silent=True) or {}
        engine = payload.get('engine')
        if not engine:
            return jsonify({'error': 'engine is required'}), 400

        container_name = configure_database(
            app_name, engine,
            version=payload.get('version'),
            db_name=payload.get('name'),
        )

        return jsonify({
            'message': f'Provisioned {engine} database for {app_name}',
            'container': container_name,
        }), 201

    except ValueError as e:
        return jsonify({'error': str(e)}), 400
    except LaunchboxError as e:
        return jsonify({'error': str(e)}), 500
    except Exception as e:
        logger.error(f"Failed to provision database for {app_name}: {e}")
        return jsonify({'error': str(e)}), 500

@app.route('/api/apps/<app_name>/database/credentials')
def api_database_credentials(app_name):
    """The database password, fetched only when explicitly asked for.

    Deliberately not rendered into the detail page: a password baked into
    the HTML ends up in the page source, the browser cache, and any
    screenshot of the page. Fetching it on demand keeps it out of all
    three until someone actually clicks reveal.

    This is not a new class of exposure -- the environment editor already
    renders an application's other secrets, and anyone who can reach this
    dashboard can already deploy and remove applications. It stays behind
    an explicit action rather than being on by default.
    """
    try:
        with StateStore() as store:
            db_row = store.get_database(app_name)

        if not db_row:
            return jsonify({'error': 'No database provisioned'}), 404

        default_ports = {'postgresql': 5432, 'mysql': 3306, 'mongodb': 27017}
        port = default_ports.get(db_row['engine'])

        database_url = (
            f"{db_row['engine']}://{db_row['username']}:"
            f"{db_row['password']}@{db_row['container_name']}:"
            f"{port}/{db_row['db_name']}"
        )

        return jsonify({
            'username': db_row['username'],
            'password': db_row['password'],
            'database_url': database_url,
            # The complete set as actually injected, so the page can reveal
            # the secret values in place and offer a copyable .env block
            # without reassembling them itself.
            'env': {
                'DATABASE_URL': database_url,
                'DB_HOST': db_row['container_name'],
                'DB_PORT': str(port),
                'DB_NAME': db_row['db_name'],
                'DB_USER': db_row['username'],
                'DB_PASSWORD': db_row['password'],
                'DB_TYPE': db_row['engine'],
            },
        })

    except Exception as e:
        logger.error(f"Failed to read credentials for {app_name}: {e}")
        return jsonify({'error': str(e)}), 500

@app.route('/api/apps/<app_name>/database', methods=['DELETE'])
def api_detach_database(app_name):
    """Stop injecting database credentials. The database and its data stay.

    To destroy the database itself, use the remove endpoint's
    ?with_database flag instead.
    """
    try:
        container_name = detach_database(app_name)

        return jsonify({
            'message': f'Detached database from {app_name}',
            'container': container_name,
        })

    except ValueError as e:
        return jsonify({'error': str(e)}), 400
    except LaunchboxError as e:
        return jsonify({'error': str(e)}), 500
    except Exception as e:
        logger.error(f"Failed to detach database from {app_name}: {e}")
        return jsonify({'error': str(e)}), 500

@app.route('/api/apps/<app_name>/stop', methods=['POST'])
def api_stop_app(app_name):
    """Stop an application"""
    try:
        client = get_docker_client()
        if not client:
            return jsonify({'error': 'Docker not available'}), 500
        
        container = client.containers.get(resolve_container_name(app_name))
        container.stop()
        
        return jsonify({'message': f'Successfully stopped {app_name}'})
    
    except docker.errors.NotFound:
        return jsonify({'error': 'Container not found'}), 404
    except Exception as e:
        logger.error(f"Failed to stop {app_name}: {e}")
        return jsonify({'error': str(e)}), 500

@app.route('/api/apps/<app_name>/start', methods=['POST'])
def api_start_app(app_name):
    """Start an application"""
    try:
        client = get_docker_client()
        if not client:
            return jsonify({'error': 'Docker not available'}), 500
        
        container = client.containers.get(resolve_container_name(app_name))
        container.start()
        
        return jsonify({'message': f'Successfully started {app_name}'})
    
    except docker.errors.NotFound:
        return jsonify({'error': 'Container not found'}), 404
    except Exception as e:
        logger.error(f"Failed to start {app_name}: {e}")
        return jsonify({'error': str(e)}), 500

@app.route('/api/apps/<app_name>/remove', methods=['DELETE'])
def api_remove_app(app_name):
    """Remove an application"""
    try:
        # remove_app owns the teardown order: route file first (so nothing is
        # routed at a container about to vanish), then containers, then this
        # app's images, then the state row. Doing it here by bare app name
        # removed no container, left every SHA-tagged image behind, and left
        # both the route file and the state row in place.
        summary = remove_app(app_name)

        return jsonify({
            'message': f'Successfully removed {app_name}',
            'removed': summary,
        })
    
    except ValueError as e:
        return jsonify({'error': str(e)}), 400
    except Exception as e:
        logger.error(f"Failed to remove {app_name}: {e}")
        return jsonify({'error': str(e)}), 500

@app.route('/api/apps/<app_name>/logs')
def api_app_logs(app_name):
    """Get application logs"""
    try:
        client = get_docker_client()
        if not client:
            return jsonify({'error': 'Docker not available'}), 500
        
        container = client.containers.get(resolve_container_name(app_name))
        logs = container.logs(tail=100).decode('utf-8')
        
        return jsonify({'logs': logs})
    
    except docker.errors.NotFound:
        return jsonify({'error': 'Container not found'}), 404
    except Exception as e:
        logger.error(f"Failed to get logs for {app_name}: {e}")
        return jsonify({'error': str(e)}), 500

@app.route('/app/<app_name>')
def app_detail(app_name):
    """Application detail page"""
    apps = get_app_list()
    app_info = next((app for app in apps if app['name'] == app_name), None)

    if not app_info:
        flash(f'Application {app_name} not found', 'error')
        return redirect(url_for('dashboard'))

    # Get detailed container info
    client = get_docker_client()
    container_info = None
    if client and app_info['status'] != 'not deployed':
        try:
            container = client.containers.get(resolve_container_name(app_name))
            container_info = {
                'id': container.id,
                'image': container.image.tags[0] if container.image.tags else 'unknown',
                'created': container.attrs['Created'],
                'started': container.attrs['State'].get('StartedAt'),
                'ports': container.attrs['NetworkSettings']['Ports'],
                'labels': container.labels
            }
        except Exception as e:
            logger.warning(f"Failed to get detailed container info: {e}")

    # Load configuration
    config_info = None
    if app_info['has_config']:
        try:
            config = LaunchboxConfig(app_info['path'])
            config_info = {
                'port': config.get_port(),
                'environment': config.get_environment_vars(),
                'resources': config.get_resource_limits(),
                'database_enabled': config.is_database_enabled(),
                'https_enabled': config.is_https_enabled()
            }
        except Exception as e:
            logger.warning(f"Failed to load config details: {e}")

    # Deployment history, which row is currently live (so the template can
    # offer "Roll back" on every successful row except that one), and the
    # database this app was provisioned with, if any.
    history = []
    current_deployment_id = None
    database_info = None
    editable_env = None
    try:
        with StateStore() as store:
            history = store.list_deployments(app_name, limit=20)
            state_row = store.get_app(app_name)
            if state_row:
                current_deployment_id = state_row.get('current_deployment')

            current_deployment = None
            if current_deployment_id:
                current_deployment = store.get_deployment(current_deployment_id)

            # Apps deployed purely via git push have no local launchbox.yaml
            # to read config_info from above -- the config actually used is
            # on their deployment row instead.
            if config_info is None and current_deployment:
                raw_config = current_deployment.get('config_json')
                if raw_config:
                    try:
                        mapping = json.loads(raw_config)
                        config_info = {
                            'port': mapping.get('app', {}).get('port', 3000),
                            'environment': {},
                            'resources': mapping.get('resources', {}),
                            'database_enabled': mapping.get('database', {}).get('enabled', False),
                            'https_enabled': mapping.get('https', {}).get('enabled', False),
                        }
                    except Exception as e:
                        logger.warning(
                            f"Failed to parse recorded config for {app_name}: {e}"
                        )

            # The editor works off what is actually running, not off a fresh
            # re-read of launchbox.yaml/.env -- those two can differ, and for
            # a git-push-only app there is no local file to re-read at all.
            # Database connection variables are excluded: they are shown in
            # their own panel below and are re-provisioned automatically, so
            # editing them here would silently desynchronise a container from
            # the database it is actually pointed at.
            injected_db_keys = []
            injected_db_env = {}
            if current_deployment:
                raw_env = current_deployment.get('env_json')
                if raw_env:
                    try:
                        resolved = json.loads(raw_env)
                        editable_env = {
                            k: v for k, v in resolved.items()
                            if k not in RESERVED_ENV_KEYS
                        }
                        # Which of the reserved keys are actually present for
                        # THIS app, not a generic list of all seven -- an app
                        # without DATABASE_URL wired up (HTTPS off, say)
                        # should not claim it has one.
                        injected_db_keys = sorted(
                            k for k in resolved if k in RESERVED_ENV_KEYS
                        )
                        # Values for the non-secret keys render directly;
                        # the two that embed the password are sent as None
                        # and fetched on demand by the reveal control.
                        injected_db_env = {
                            k: (None if k in SECRET_DB_KEYS else v)
                            for k, v in sorted(resolved.items())
                            if k in RESERVED_ENV_KEYS
                        }
                    except Exception as e:
                        logger.warning(
                            f"Failed to parse recorded environment for {app_name}: {e}"
                        )

            db_row = store.get_database(app_name)
            if db_row:
                default_ports = {
                    'postgresql': 5432, 'mysql': 3306, 'mongodb': 27017,
                }
                database_info = {
                    'engine': db_row['engine'],
                    'host': db_row['container_name'],
                    'port': default_ports.get(db_row['engine']),
                    'name': db_row['db_name'],
                    'username': db_row['username'],
                    # Password is deliberately never sent to the browser --
                    # redacted here rather than omitted, so the rest of the
                    # connection string is still there to copy.
                    'database_url_redacted': (
                        f"{db_row['engine']}://{db_row['username']}:***@"
                        f"{db_row['container_name']}:"
                        f"{default_ports.get(db_row['engine'])}/{db_row['db_name']}"
                    ),
                    'injected_keys': injected_db_keys,
                    'injected_env': injected_db_env,
                    'has_volume': bool(db_row.get('volume_name')),
                }
    except Exception as e:
        logger.warning(f"Failed to load deployment history for {app_name}: {e}")

    repo_path = os.path.join(REPOS_DIR, f"{app_name}.git") if app_info['has_repo'] else None

    return render_template('app_detail.html',
                         app=app_info,
                         container=container_info,
                         config=config_info,
                         history=history,
                         current_deployment_id=current_deployment_id,
                         database=database_info,
                         repo_path=repo_path,
                         editable_env=editable_env,
                         database_engines=sorted(DATABASE_ENGINES.keys()),
                         database_engine_defaults=DATABASE_ENGINES)

def main():
    """Entry point used by ``python3 -m launchbox dashboard``."""
    app.run(host="0.0.0.0", port=8000, debug=True)


if __name__ == '__main__':
    main()