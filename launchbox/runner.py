import subprocess
import sys
import os
from launchbox.config import APPS_DIR
from launchbox.config_parser import LaunchboxConfig
from launchbox.logger import setup_logger, DeploymentError
from launchbox.database_manager import DatabaseManager

logger = setup_logger("runner")

def run(app_name: str) -> bool:
    """Run Docker container for the application"""
    try:
        app_path = f"{APPS_DIR}/{app_name}"
        
        # Validate app directory exists
        if not os.path.exists(app_path):
            raise DeploymentError(f"Application directory not found: {app_path}")
        
        # Load configuration
        config = LaunchboxConfig(app_path)
        port = config.get_port()
        env_vars = config.get_environment_vars()
        resource_limits = config.get_resource_limits()
        health_check = config.get_health_check()
        
        # Setup database if needed
        db_manager = DatabaseManager()
        db_connection_info = db_manager.create_database_for_app(app_name, app_path)
        if db_connection_info:
            logger.info(f"Database configured for {app_name}")
            env_vars.update(db_connection_info)
        
        image_name = f"launchbox-{app_name}"
        
        logger.info(f"Deploying application: {app_name}")
        logger.info(f"Image: {image_name}")
        logger.info(f"Port: {port}")
        
        # Remove existing container if it exists
        logger.debug(f"Removing existing container: {app_name}")
        subprocess.run(
            ["docker", "rm", "-f", app_name],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        
        # Build docker run command
        run_cmd = [
            "docker", "run", "-d",
            "--name", app_name,
            "--network", "traefik_default",
            "--label", "traefik.enable=true",
            "--label", f"traefik.http.routers.{app_name}.rule=Host(`{app_name}.localhost`)",
            "--label", f"traefik.http.services.{app_name}.loadbalancer.server.port={port}",
        ]
        
        # Add environment variables
        for key, value in env_vars.items():
            run_cmd.extend(["-e", f"{key}={value}"])
            logger.debug(f"Environment variable: {key}={value}")
        
        # Add resource limits
        for key, value in resource_limits.items():
            run_cmd.extend([f"--{key}", value])
            logger.debug(f"Resource limit: --{key} {value}")
        
        # Add health check if configured
        if health_check:
            # Use Python-based health check that works in all containers
            health_path = health_check.get('path', '/health')
            interval = health_check.get('interval', '30s')
            timeout = health_check.get('timeout', '10s')
            retries = health_check.get('retries', 3)
            
            # Create a simple Python health check that doesn't require curl
            health_cmd = f"python3 -c \"import urllib.request, sys; urllib.request.urlopen('http://localhost:{port}{health_path}', timeout=5)\""
            
            run_cmd.extend([
                "--health-cmd", health_cmd,
                "--health-interval", interval,
                "--health-timeout", timeout,
                "--health-retries", str(retries)
            ])
            logger.debug(f"Health check enabled (Python-based): {health_cmd}")
        
        # Add HTTPS labels if enabled
        if config.is_https_enabled():
            run_cmd.extend([
                "--label", f"traefik.http.routers.{app_name}-secure.rule=Host(`{app_name}.localhost`)",
                "--label", f"traefik.http.routers.{app_name}-secure.tls=true",
                "--label", f"traefik.http.routers.{app_name}-secure.entrypoints=websecure"
            ])
            
            if config.should_redirect_http():
                run_cmd.extend([
                    "--label", f"traefik.http.routers.{app_name}.middlewares={app_name}-redirect",
                    "--label", f"traefik.http.middlewares.{app_name}-redirect.redirectscheme.scheme=https"
                ])
            logger.debug("HTTPS labels added")
        
        # Add the image name
        run_cmd.append(image_name)
        
        logger.debug(f"Running command: {' '.join(run_cmd)}")
        
        # Run the container
        result = subprocess.run(
            run_cmd,
            capture_output=True,
            text=True,
            check=False
        )
        
        if result.returncode != 0:
            logger.error(f"Docker run failed for {app_name}")
            logger.error(f"stdout: {result.stdout}")
            logger.error(f"stderr: {result.stderr}")
            raise DeploymentError(f"Docker run failed: {result.stderr}")
        
        container_id = result.stdout.strip()
        logger.info(f"Successfully deployed {app_name} (container: {container_id[:12]})")
        logger.info(f"Application available at: http://{app_name}.localhost")
        
        if config.is_https_enabled():
            logger.info(f"HTTPS available at: https://{app_name}.localhost")
        
        return True
        
    except DeploymentError:
        raise
    except Exception as e:
        logger.error(f"Unexpected error deploying {app_name}: {e}")
        raise DeploymentError(f"Unexpected deployment error: {e}")

if __name__ == "__main__":
    if len(sys.argv) < 2:
        logger.error("Usage: runner.py <app_name>")
        sys.exit(1)
    
    try:
        run(sys.argv[1])
        sys.exit(0)
    except DeploymentError as e:
        logger.error(f"Deployment failed: {e}")
        sys.exit(1)
