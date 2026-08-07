from flask import Flask, render_template, request, jsonify, flash, redirect, url_for
import docker
import subprocess
import os
from pathlib import Path
from datetime import datetime
from launchbox.config import APPS_DIR, REPOS_DIR
from launchbox.config_parser import LaunchboxConfig
from launchbox.deploy import deploy, remove_app, rollback
from launchbox.ssl_manager import SSLManager
from launchbox.logger import setup_logger, LaunchboxError
from launchbox.state import StateStore

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

def get_app_list():
    """Get list of applications"""
    apps = []
    apps_path = Path(APPS_DIR)

    if not apps_path.exists():
        return apps

    # One store for the whole listing rather than one per app: each
    # resolve_container_name call and each health lookup below would
    # otherwise open its own sqlite connection.
    try:
        store = StateStore()
    except Exception as e:
        logger.warning(f"Failed to open state store: {e}")
        store = None

    try:
        for app_dir in apps_path.iterdir():
            if app_dir.is_dir():
                app_info = {
                    'name': app_dir.name,
                    'path': str(app_dir),
                    'has_dockerfile': (app_dir / 'Dockerfile').exists(),
                    'has_config': (app_dir / 'launchbox.yaml').exists(),
                    'status': 'unknown',
                    'container_id': None,
                    'created': None,
                    'port': 3000,
                    'url': f"http://{app_dir.name}.localhost",
                    'health_state': None,
                    'marked_failed': False,
                }

                # Get configuration if available
                if app_info['has_config']:
                    try:
                        config = LaunchboxConfig(str(app_dir))
                        app_info['port'] = config.get_port()
                        if config.is_https_enabled():
                            app_info['url'] = f"https://{app_dir.name}.localhost"
                    except Exception as e:
                        logger.warning(f"Failed to load config for {app_dir.name}: {e}")

                # Supervisor-tracked health, independent of Docker's own
                # container status below -- this is what lets the dashboard
                # show an app the supervisor gave up restarting.
                if store is not None:
                    try:
                        state_row = store.get_app(app_dir.name)
                        if state_row:
                            app_info['health_state'] = state_row.get('health_state')
                            app_info['marked_failed'] = bool(state_row.get('marked_failed'))
                    except Exception as e:
                        logger.warning(f"Failed to read state for {app_dir.name}: {e}")

                # Get container status
                client = get_docker_client()
                if client:
                    try:
                        container = client.containers.get(
                            resolve_container_name(app_dir.name, store=store)
                        )
                        app_info['status'] = container.status
                        app_info['container_id'] = container.id[:12]
                        app_info['created'] = container.attrs['Created']
                    except docker.errors.NotFound:
                        app_info['status'] = 'not deployed'
                    except Exception as e:
                        logger.warning(f"Failed to get container info for {app_dir.name}: {e}")

                apps.append(app_info)
    finally:
        if store is not None:
            store.close()

    return sorted(apps, key=lambda x: x['name'])

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
    
    return render_template('dashboard.html', apps=apps, docker_info=docker_info)

@app.route('/api/apps')
def api_apps():
    """API endpoint to get apps"""
    return jsonify(get_app_list())

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

    # Deployment history and which row is currently live, so the template can
    # offer "Roll back" on every successful row except the current one.
    history = []
    current_deployment_id = None
    try:
        with StateStore() as store:
            history = store.list_deployments(app_name, limit=20)
            state_row = store.get_app(app_name)
            if state_row:
                current_deployment_id = state_row.get('current_deployment')
    except Exception as e:
        logger.warning(f"Failed to load deployment history for {app_name}: {e}")

    return render_template('app_detail.html',
                         app=app_info,
                         container=container_info,
                         config=config_info,
                         history=history,
                         current_deployment_id=current_deployment_id)

def main():
    """Entry point used by ``python3 -m launchbox dashboard``."""
    app.run(host="0.0.0.0", port=8000, debug=True)


if __name__ == '__main__':
    main()