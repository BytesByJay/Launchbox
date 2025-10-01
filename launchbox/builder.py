import subprocess
import sys
import os
from pathlib import Path
from launchbox.config import APPS_DIR
from launchbox.config_parser import LaunchboxConfig
from launchbox.logger import setup_logger, BuildError

logger = setup_logger("builder")

def build(app_name: str) -> bool:
    """Build Docker image for the application"""
    try:
        app_path = f"{APPS_DIR}/{app_name}"
        
        # Validate app directory exists
        if not os.path.exists(app_path):
            raise BuildError(f"Application directory not found: {app_path}")
        
        # Load configuration
        config = LaunchboxConfig(app_path)
        dockerfile = config.get_dockerfile()
        build_context = config.get_build_context()
        
        # Validate Dockerfile exists
        dockerfile_path = os.path.join(app_path, dockerfile)
        if not os.path.exists(dockerfile_path):
            raise BuildError(f"Dockerfile not found: {dockerfile_path}")
        
        image_name = f"launchbox-{app_name}"
        build_path = os.path.join(app_path, build_context)
        
        logger.info(f"Building image: {image_name}")
        logger.info(f"Build context: {build_path}")
        logger.info(f"Dockerfile: {dockerfile}")
        
        # Build Docker image
        build_cmd = [
            "docker", "build", 
            "-t", image_name,
            "-f", dockerfile_path,
            build_path
        ]
        
        logger.debug(f"Running command: {' '.join(build_cmd)}")
        result = subprocess.run(
            build_cmd, 
            capture_output=True, 
            text=True, 
            check=False
        )
        
        if result.returncode != 0:
            logger.error(f"Docker build failed for {app_name}")
            logger.error(f"stdout: {result.stdout}")
            logger.error(f"stderr: {result.stderr}")
            raise BuildError(f"Docker build failed: {result.stderr}")
        
        logger.info(f"Successfully built image: {image_name}")
        return True
        
    except BuildError:
        raise
    except Exception as e:
        logger.error(f"Unexpected error building {app_name}: {e}")
        raise BuildError(f"Unexpected build error: {e}")

if __name__ == "__main__":
    if len(sys.argv) < 2:
        logger.error("Usage: builder.py <app_name>")
        sys.exit(1)
    
    try:
        build(sys.argv[1])
        sys.exit(0)
    except BuildError as e:
        logger.error(f"Build failed: {e}")
        sys.exit(1)
