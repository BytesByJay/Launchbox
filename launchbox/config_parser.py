import copy
import os
import yaml
from typing import Dict, Any, Optional, List
import logging

logger = logging.getLogger(__name__)

class LaunchboxConfig:
    """Configuration parser for launchbox.yaml files"""
    
    def __init__(self, app_path: str):
        self.app_path = app_path
        self.config_path = os.path.join(app_path, "launchbox.yaml")
        self.config = self._load_config()

    @classmethod
    def from_mapping(cls, mapping: Dict[str, Any]) -> "LaunchboxConfig":
        """Build a config around an already-merged mapping, touching no files.

        This is how a rollback reproduces the configuration a past deployment
        actually ran with. The mapping comes from the deployment row, not from
        ``apps/<app>/launchbox.yaml`` -- which for a git-pushed application
        never exists, so reading it would silently yield built-in defaults
        (port 3000, no health check, no resource limits, no environment).

        ``app_path`` is deliberately None: nothing here may reach the
        filesystem, and get_environment_vars must not pick up a stray ``.env``
        relative to the process's working directory.
        """
        instance = cls.__new__(cls)
        instance.app_path = None
        instance.config_path = None
        # Merged under the defaults so a mapping stored by an older version,
        # missing a key added since, still yields a complete config.
        instance.config = instance._deep_merge(
            cls._default_config(), copy.deepcopy(dict(mapping or {}))
        )
        return instance

    @staticmethod
    def _default_config() -> Dict[str, Any]:
        """The built-in configuration every launchbox.yaml is merged over."""
        return {
            "app": {
                "port": 3000,
                "health_check": None,
                "build": {
                    "dockerfile": "Dockerfile",
                    "context": "."
                }
            },
            "resources": {
                "memory": None,
                "cpu": None
            },
            "environment": {},
            "database": {
                "enabled": False,
                "type": "postgresql",
                "version": "13",
                "name": None
            },
            "https": {
                "enabled": False,
                "redirect_http": True
            }
        }

    def _load_config(self) -> Dict[str, Any]:
        """Load and parse launchbox.yaml configuration file"""
        default_config = self._default_config()
        
        if not os.path.exists(self.config_path):
            logger.info(f"No launchbox.yaml found in {self.app_path}, using defaults")
            return default_config
        
        try:
            with open(self.config_path, 'r') as f:
                user_config = yaml.safe_load(f) or {}
            
            # Merge user config with defaults
            config = self._deep_merge(default_config, user_config)
            logger.info(f"Loaded configuration from {self.config_path}")
            return config
        
        except Exception as e:
            logger.error(f"Error loading configuration: {e}")
            return default_config
    
    def _deep_merge(self, base: Dict, override: Dict) -> Dict:
        """Deep merge two dictionaries"""
        result = base.copy()
        for key, value in override.items():
            if key in result and isinstance(result[key], dict) and isinstance(value, dict):
                result[key] = self._deep_merge(result[key], value)
            else:
                result[key] = value
        return result
    
    def get_port(self) -> int:
        """Get application port"""
        return self.config["app"]["port"]
    
    def get_dockerfile(self) -> str:
        """Get Dockerfile path"""
        return self.config["app"]["build"]["dockerfile"]
    
    def get_build_context(self) -> str:
        """Get build context path"""
        return self.config["app"]["build"]["context"]
    
    def get_environment_vars(self) -> Dict[str, str]:
        """Get environment variables"""
        env_vars = self.config["environment"].copy()
        
        # Load .env file if it exists. A config built by from_mapping has no
        # app_path at all, and must not fall back to a relative ".env".
        env_file = os.path.join(self.app_path, ".env") if self.app_path else None
        if env_file and os.path.exists(env_file):
            try:
                with open(env_file, 'r') as f:
                    for line in f:
                        line = line.strip()
                        if line and not line.startswith('#') and '=' in line:
                            key, value = line.split('=', 1)
                            env_vars[key.strip()] = value.strip()
            except Exception as e:
                logger.error(f"Error loading .env file: {e}")
        
        return env_vars
    
    def get_resource_limits(self) -> Dict[str, str]:
        """Get resource limits for Docker"""
        limits = {}
        
        if self.config["resources"]["memory"]:
            limits["memory"] = self.config["resources"]["memory"]
        
        if self.config["resources"]["cpu"]:
            limits["cpus"] = str(self.config["resources"]["cpu"])
        
        return limits
    
    def get_health_check(self) -> Optional[Dict[str, Any]]:
        """Get health check configuration"""
        return self.config["app"]["health_check"]
    
    def is_database_enabled(self) -> bool:
        """Check if database is enabled"""
        return self.config["database"]["enabled"]
    
    def get_database_config(self) -> Dict[str, Any]:
        """Get database configuration"""
        return self.config["database"]
    
    def is_https_enabled(self) -> bool:
        """Check if HTTPS is enabled"""
        return self.config["https"]["enabled"]
    
    def should_redirect_http(self) -> bool:
        """Check if HTTP should redirect to HTTPS"""
        return self.config["https"]["redirect_http"]
    
    def generate_example_config(self) -> str:
        """Generate example configuration file content"""
        return """# Launchbox Application Configuration
# This file is optional - all settings have sensible defaults

app:
  # Port your application runs on (default: 3000)
  port: 3000
  
  # Health check configuration (optional)
  health_check:
    path: "/health"
    interval: "30s"
    timeout: "10s"
    retries: 3
  
  # Build configuration
  build:
    dockerfile: "Dockerfile"
    context: "."

# Resource limits (optional)
resources:
  memory: "512m"    # e.g., "256m", "1g"
  cpu: 0.5          # e.g., 0.5 = half a CPU core

# Environment variables
environment:
  NODE_ENV: production
  DEBUG: false

# Database configuration
database:
  enabled: false
  type: postgresql    # postgresql, mysql, mongodb
  version: "13"       # database version
  name: myapp_db      # database name (defaults to app name)

# HTTPS configuration
https:
  enabled: false
  redirect_http: true
"""