import subprocess
import os
import shutil
from pathlib import Path
from typing import List, Optional
from launchbox.logger import setup_logger, LaunchboxError

logger = setup_logger("ssl_manager")

class SSLError(LaunchboxError):
    """SSL-related errors"""
    pass

class SSLManager:
    """Manage SSL certificates using mkcert"""
    
    def __init__(self):
        self.cert_dir = Path("certs")
        self.cert_dir.mkdir(exist_ok=True)
        
    def is_mkcert_installed(self) -> bool:
        """Check if mkcert is installed"""
        return shutil.which("mkcert") is not None
    
    def install_mkcert(self) -> bool:
        """Install mkcert if not present"""
        if self.is_mkcert_installed():
            logger.info("mkcert is already installed")
            return True
        
        logger.info("Installing mkcert...")
        
        # Try to install mkcert based on the system
        try:
            # Check if we're on a system with package managers
            if shutil.which("brew"):
                result = subprocess.run(["brew", "install", "mkcert"], check=True)
            elif shutil.which("yum"):
                logger.error("mkcert installation via yum not directly supported")
                logger.info("Please install mkcert manually from: https://github.com/FiloSottile/mkcert")
                return False
            elif shutil.which("apt"):
                # Download and install mkcert
                download_cmd = [
                    "curl", "-JLO", 
                    "https://dl.filippo.io/mkcert/latest?for=linux/amd64"
                ]
                subprocess.run(download_cmd, check=True)
                subprocess.run(["chmod", "+x", "mkcert-*-linux-amd64"], shell=True, check=True)
                subprocess.run(["sudo", "mv", "mkcert-*-linux-amd64", "/usr/local/bin/mkcert"], shell=True, check=True)
            else:
                logger.error("Unable to automatically install mkcert")
                logger.info("Please install mkcert manually from: https://github.com/FiloSottile/mkcert")
                return False
            
            return self.is_mkcert_installed()
        
        except Exception as e:
            logger.error(f"Failed to install mkcert: {e}")
            return False
    
    def setup_ca(self) -> bool:
        """Setup mkcert CA"""
        if not self.is_mkcert_installed():
            if not self.install_mkcert():
                raise SSLError("mkcert is not installed and could not be installed automatically")
        
        try:
            logger.info("Setting up mkcert CA...")
            result = subprocess.run(
                ["mkcert", "-install"],
                capture_output=True,
                text=True,
                check=True
            )
            logger.info("mkcert CA setup completed")
            return True
        
        except subprocess.CalledProcessError as e:
            logger.error(f"Failed to setup mkcert CA: {e.stderr}")
            raise SSLError(f"mkcert CA setup failed: {e.stderr}")
    
    def create_certificate(self, domains: List[str]) -> tuple[str, str]:
        """Create SSL certificate for domains"""
        if not self.is_mkcert_installed():
            self.setup_ca()
        
        try:
            # Create certificate filename
            primary_domain = domains[0]
            cert_name = primary_domain.replace(".", "_")
            cert_file = self.cert_dir / f"{cert_name}.pem"
            key_file = self.cert_dir / f"{cert_name}-key.pem"
            
            # Generate certificate
            logger.info(f"Creating SSL certificate for: {', '.join(domains)}")
            cmd = ["mkcert", "-cert-file", str(cert_file), "-key-file", str(key_file)] + domains
            
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                check=True,
                cwd=self.cert_dir
            )
            
            logger.info(f"SSL certificate created: {cert_file}")
            return str(cert_file), str(key_file)
        
        except subprocess.CalledProcessError as e:
            logger.error(f"Failed to create certificate: {e.stderr}")
            raise SSLError(f"Certificate creation failed: {e.stderr}")
    
    def get_certificate_for_app(self, app_name: str) -> tuple[str, str]:
        """Get or create certificate for application"""
        domains = [f"{app_name}.localhost", "localhost"]
        cert_name = f"{app_name}_localhost"
        cert_file = self.cert_dir / f"{cert_name}.pem"
        key_file = self.cert_dir / f"{cert_name}-key.pem"
        
        # Check if certificate already exists
        if cert_file.exists() and key_file.exists():
            logger.info(f"Using existing certificate for {app_name}")
            return str(cert_file), str(key_file)
        
        # Create new certificate
        return self.create_certificate(domains)
    
    def update_traefik_config_for_https(self) -> None:
        """Update Traefik configuration to support HTTPS"""
        traefik_dir = Path("traefik")
        traefik_dir.mkdir(exist_ok=True)
        
        # Create dynamic configuration for SSL
        dynamic_config = traefik_dir / "dynamic.yml"
        
        config_content = f"""# Dynamic configuration for HTTPS
tls:
  certificates:
"""
        
        # Add all certificates from cert directory
        for cert_file in self.cert_dir.glob("*.pem"):
            if not cert_file.name.endswith("-key.pem"):
                key_file = cert_file.with_name(cert_file.name.replace(".pem", "-key.pem"))
                if key_file.exists():
                    config_content += f"""    - certFile: /certs/{cert_file.name}
      keyFile: /certs/{key_file.name}
"""
        
        dynamic_config.write_text(config_content)
        logger.info(f"Updated Traefik dynamic configuration: {dynamic_config}")

def setup_https_support() -> SSLManager:
    """Initialize and setup HTTPS support"""
    ssl_manager = SSLManager()
    
    try:
        ssl_manager.setup_ca()
        ssl_manager.update_traefik_config_for_https()
        logger.info("HTTPS support initialized")
        return ssl_manager
    
    except Exception as e:
        logger.error(f"Failed to setup HTTPS support: {e}")
        raise