from flask import Flask, render_template, request, jsonify, flash, redirect, url_for
import docker
import subprocess
import os
from pathlib import Path
from datetime import datetime
from launchbox.config import APPS_DIR, REPOS_DIR
from launchbox.config_parser import LaunchboxConfig
from launchbox.builder import build
from launchbox.runner import run
from launchbox.ssl_manager import SSLManager
from launchbox.logger import setup_logger, LaunchboxError

logger = setup_logger("dashboard")

app = Flask(__name__)
app.secret_key = 'launchbox-dashboard-key'

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
                'url': f"http://{app_dir.name}.localhost"
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
            
            # Get container status
            client = get_docker_client()
            if client:
                try:
                    container = client.containers.get(app_dir.name)
                    app_info['status'] = container.status
                    app_info['container_id'] = container.id[:12]
                    app_info['created'] = container.attrs['Created']
                except docker.errors.NotFound:
                    app_info['status'] = 'not deployed'
                except Exception as e:
                    logger.warning(f"Failed to get container info for {app_dir.name}: {e}")
            
            apps.append(app_info)
    
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
        
        # Build the application
        build_success = build(app_name)
        if not build_success:
            return jsonify({'error': 'Build failed'}), 500
        
        # Run the application
        run_success = run(app_name)
        if not run_success:
            return jsonify({'error': 'Deployment failed'}), 500
        
        return jsonify({'message': f'Successfully deployed {app_name}'})
    
    except Exception as e:
        logger.error(f"Failed to deploy {app_name}: {e}")
        return jsonify({'error': str(e)}), 500

@app.route('/api/apps/<app_name>/stop', methods=['POST'])
def api_stop_app(app_name):
    """Stop an application"""
    try:
        client = get_docker_client()
        if not client:
            return jsonify({'error': 'Docker not available'}), 500
        
        container = client.containers.get(app_name)
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
        
        container = client.containers.get(app_name)
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
        client = get_docker_client()
        if client:
            try:
                container = client.containers.get(app_name)
                container.stop()
                container.remove()
            except docker.errors.NotFound:
                pass  # Container doesn't exist, that's okay
        
        # Remove Docker image
        try:
            if client:
                client.images.remove(f'launchbox-{app_name}', force=True)
        except Exception:
            pass  # Image might not exist
        
        return jsonify({'message': f'Successfully removed {app_name}'})
    
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
        
        container = client.containers.get(app_name)
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
            container = client.containers.get(app_name)
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
    
    return render_template('app_detail.html', 
                         app=app_info, 
                         container=container_info, 
                         config=config_info)

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=8000, debug=True)