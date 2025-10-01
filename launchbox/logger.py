import logging
import sys
from pathlib import Path
import os

def setup_logger(name: str, level: str = "INFO") -> logging.Logger:
    """Setup logger with consistent formatting"""
    
    # Create logs directory if it doesn't exist
    log_dir = Path("logs")
    log_dir.mkdir(exist_ok=True)
    
    # Configure logger
    logger = logging.getLogger(name)
    logger.setLevel(getattr(logging, level.upper(), logging.INFO))
    
    # Prevent duplicate handlers
    if logger.handlers:
        return logger
    
    # Console handler
    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setLevel(logging.INFO)
    console_format = logging.Formatter(
        '%(asctime)s [%(levelname)s] %(name)s: %(message)s',
        datefmt='%H:%M:%S'
    )
    console_handler.setFormatter(console_format)
    logger.addHandler(console_handler)
    
    # File handler
    file_handler = logging.FileHandler(log_dir / f"{name}.log")
    file_handler.setLevel(logging.DEBUG)
    file_format = logging.Formatter(
        '%(asctime)s [%(levelname)s] %(name)s:%(lineno)d: %(message)s',
        datefmt='%Y-%m-%d %H:%M:%S'
    )
    file_handler.setFormatter(file_format)
    logger.addHandler(file_handler)
    
    return logger

class LaunchboxError(Exception):
    """Base exception for Launchbox errors"""
    pass

class ConfigurationError(LaunchboxError):
    """Configuration-related errors"""
    pass

class BuildError(LaunchboxError):
    """Docker build errors"""
    pass

class DeploymentError(LaunchboxError):
    """Deployment-related errors"""
    pass