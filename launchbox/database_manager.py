import docker
import subprocess
import time
from typing import Dict, Optional, List
from launchbox.logger import setup_logger, LaunchboxError
from launchbox.config_parser import LaunchboxConfig

logger = setup_logger("database_manager")

class DatabaseError(LaunchboxError):
    """Database-related errors"""
    pass

class DatabaseManager:
    """Manage database containers for applications"""
    
    def __init__(self):
        self.client = self._get_docker_client()
        
    def _get_docker_client(self):
        """Get Docker client"""
        try:
            return docker.from_env()
        except Exception as e:
            logger.error(f"Failed to connect to Docker: {e}")
            raise DatabaseError(f"Docker connection failed: {e}")
    
    def create_database_for_app(self, app_name: str, app_path: str) -> Optional[Dict[str, str]]:
        """Create database container for application if needed"""
        config = LaunchboxConfig(app_path)
        
        if not config.is_database_enabled():
            logger.debug(f"Database not enabled for {app_name}")
            return None
        
        db_config = config.get_database_config()
        db_type = db_config.get('type', 'postgresql')
        db_version = db_config.get('version', '13')
        db_name = db_config.get('name', f"{app_name}_db")
        
        logger.info(f"Setting up {db_type} database for {app_name}")
        
        if db_type == 'postgresql':
            return self._create_postgresql(app_name, db_name, db_version)
        elif db_type == 'mysql':
            return self._create_mysql(app_name, db_name, db_version)
        elif db_type == 'mongodb':
            return self._create_mongodb(app_name, db_name, db_version)
        else:
            raise DatabaseError(f"Unsupported database type: {db_type}")
    
    def _create_postgresql(self, app_name: str, db_name: str, version: str) -> Dict[str, str]:
        """Create PostgreSQL container"""
        container_name = f"{app_name}_postgres"
        
        try:
            # Check if container already exists
            try:
                container = self.client.containers.get(container_name)
                if container.status == 'running':
                    logger.info(f"PostgreSQL container {container_name} already running")
                    return self._get_postgres_connection_info(container_name, db_name)
                else:
                    container.start()
                    logger.info(f"Started existing PostgreSQL container {container_name}")
                    return self._get_postgres_connection_info(container_name, db_name)
            except docker.errors.NotFound:
                pass
            
            # Create new container
            logger.info(f"Creating PostgreSQL container: {container_name}")
            
            environment = {
                'POSTGRES_DB': db_name,
                'POSTGRES_USER': 'launchbox',
                'POSTGRES_PASSWORD': 'launchbox123',
                'POSTGRES_HOST_AUTH_METHOD': 'trust'
            }
            
            container = self.client.containers.run(
                f"postgres:{version}",
                name=container_name,
                environment=environment,
                network='traefik_default',
                detach=True,
                labels={
                    'launchbox.app': app_name,
                    'launchbox.service': 'database',
                    'launchbox.db_type': 'postgresql'
                }
            )
            
            # Wait for database to be ready
            self._wait_for_postgres(container_name)
            
            logger.info(f"PostgreSQL container {container_name} created and ready")
            return self._get_postgres_connection_info(container_name, db_name)
            
        except Exception as e:
            logger.error(f"Failed to create PostgreSQL container: {e}")
            raise DatabaseError(f"PostgreSQL setup failed: {e}")
    
    def _create_mysql(self, app_name: str, db_name: str, version: str) -> Dict[str, str]:
        """Create MySQL container"""
        container_name = f"{app_name}_mysql"
        
        try:
            # Check if container already exists
            try:
                container = self.client.containers.get(container_name)
                if container.status == 'running':
                    logger.info(f"MySQL container {container_name} already running")
                    return self._get_mysql_connection_info(container_name, db_name)
                else:
                    container.start()
                    logger.info(f"Started existing MySQL container {container_name}")
                    return self._get_mysql_connection_info(container_name, db_name)
            except docker.errors.NotFound:
                pass
            
            # Create new container
            logger.info(f"Creating MySQL container: {container_name}")
            
            environment = {
                'MYSQL_DATABASE': db_name,
                'MYSQL_USER': 'launchbox',
                'MYSQL_PASSWORD': 'launchbox123',
                'MYSQL_ROOT_PASSWORD': 'rootpass123'
            }
            
            container = self.client.containers.run(
                f"mysql:{version}",
                name=container_name,
                environment=environment,
                network='traefik_default',
                detach=True,
                labels={
                    'launchbox.app': app_name,
                    'launchbox.service': 'database',
                    'launchbox.db_type': 'mysql'
                }
            )
            
            # Wait for database to be ready
            self._wait_for_mysql(container_name)
            
            logger.info(f"MySQL container {container_name} created and ready")
            return self._get_mysql_connection_info(container_name, db_name)
            
        except Exception as e:
            logger.error(f"Failed to create MySQL container: {e}")
            raise DatabaseError(f"MySQL setup failed: {e}")
    
    def _create_mongodb(self, app_name: str, db_name: str, version: str) -> Dict[str, str]:
        """Create MongoDB container"""
        container_name = f"{app_name}_mongodb"
        
        try:
            # Check if container already exists
            try:
                container = self.client.containers.get(container_name)
                if container.status == 'running':
                    logger.info(f"MongoDB container {container_name} already running")
                    return self._get_mongodb_connection_info(container_name, db_name)
                else:
                    container.start()
                    logger.info(f"Started existing MongoDB container {container_name}")
                    return self._get_mongodb_connection_info(container_name, db_name)
            except docker.errors.NotFound:
                pass
            
            # Create new container
            logger.info(f"Creating MongoDB container: {container_name}")
            
            environment = {
                'MONGO_INITDB_DATABASE': db_name,
                'MONGO_INITDB_ROOT_USERNAME': 'launchbox',
                'MONGO_INITDB_ROOT_PASSWORD': 'launchbox123'
            }
            
            container = self.client.containers.run(
                f"mongo:{version}",
                name=container_name,
                environment=environment,
                network='traefik_default',
                detach=True,
                labels={
                    'launchbox.app': app_name,
                    'launchbox.service': 'database',
                    'launchbox.db_type': 'mongodb'
                }
            )
            
            # Wait for database to be ready
            self._wait_for_mongodb(container_name)
            
            logger.info(f"MongoDB container {container_name} created and ready")
            return self._get_mongodb_connection_info(container_name, db_name)
            
        except Exception as e:
            logger.error(f"Failed to create MongoDB container: {e}")
            raise DatabaseError(f"MongoDB setup failed: {e}")
    
    def _wait_for_postgres(self, container_name: str, timeout: int = 30):
        """Wait for PostgreSQL to be ready"""
        logger.info(f"Waiting for PostgreSQL {container_name} to be ready...")
        
        for i in range(timeout):
            try:
                result = self.client.containers.get(container_name).exec_run(
                    "pg_isready -U launchbox"
                )
                if result.exit_code == 0:
                    logger.info(f"PostgreSQL {container_name} is ready")
                    return
            except Exception:
                pass
            
            time.sleep(1)
        
        raise DatabaseError(f"PostgreSQL {container_name} failed to become ready within {timeout} seconds")
    
    def _wait_for_mysql(self, container_name: str, timeout: int = 30):
        """Wait for MySQL to be ready"""
        logger.info(f"Waiting for MySQL {container_name} to be ready...")
        
        for i in range(timeout):
            try:
                result = self.client.containers.get(container_name).exec_run(
                    "mysqladmin ping -h localhost -u launchbox -plaunchbox123"
                )
                if result.exit_code == 0:
                    logger.info(f"MySQL {container_name} is ready")
                    return
            except Exception:
                pass
            
            time.sleep(1)
        
        raise DatabaseError(f"MySQL {container_name} failed to become ready within {timeout} seconds")
    
    def _wait_for_mongodb(self, container_name: str, timeout: int = 30):
        """Wait for MongoDB to be ready"""
        logger.info(f"Waiting for MongoDB {container_name} to be ready...")
        
        for i in range(timeout):
            try:
                result = self.client.containers.get(container_name).exec_run(
                    "mongo --eval 'db.runCommand(\"ping\").ok'"
                )
                if result.exit_code == 0:
                    logger.info(f"MongoDB {container_name} is ready")
                    return
            except Exception:
                pass
            
            time.sleep(1)
        
        raise DatabaseError(f"MongoDB {container_name} failed to become ready within {timeout} seconds")
    
    def _get_postgres_connection_info(self, container_name: str, db_name: str) -> Dict[str, str]:
        """Get PostgreSQL connection information"""
        return {
            'DATABASE_URL': f"postgresql://launchbox:launchbox123@{container_name}:5432/{db_name}",
            'DB_HOST': container_name,
            'DB_PORT': '5432',
            'DB_NAME': db_name,
            'DB_USER': 'launchbox',
            'DB_PASSWORD': 'launchbox123',
            'DB_TYPE': 'postgresql'
        }
    
    def _get_mysql_connection_info(self, container_name: str, db_name: str) -> Dict[str, str]:
        """Get MySQL connection information"""
        return {
            'DATABASE_URL': f"mysql://launchbox:launchbox123@{container_name}:3306/{db_name}",
            'DB_HOST': container_name,
            'DB_PORT': '3306',
            'DB_NAME': db_name,
            'DB_USER': 'launchbox',
            'DB_PASSWORD': 'launchbox123',
            'DB_TYPE': 'mysql'
        }
    
    def _get_mongodb_connection_info(self, container_name: str, db_name: str) -> Dict[str, str]:
        """Get MongoDB connection information"""
        return {
            'DATABASE_URL': f"mongodb://launchbox:launchbox123@{container_name}:27017/{db_name}",
            'DB_HOST': container_name,
            'DB_PORT': '27017',
            'DB_NAME': db_name,
            'DB_USER': 'launchbox',
            'DB_PASSWORD': 'launchbox123',
            'DB_TYPE': 'mongodb'
        }
    
    def remove_database_for_app(self, app_name: str) -> bool:
        """Remove database containers for application"""
        try:
            removed = False
            
            # Find and remove database containers
            for db_type in ['postgres', 'mysql', 'mongodb']:
                container_name = f"{app_name}_{db_type}"
                try:
                    container = self.client.containers.get(container_name)
                    container.stop()
                    container.remove()
                    logger.info(f"Removed database container: {container_name}")
                    removed = True
                except docker.errors.NotFound:
                    continue
                except Exception as e:
                    logger.warning(f"Failed to remove {container_name}: {e}")
            
            return removed
            
        except Exception as e:
            logger.error(f"Failed to remove databases for {app_name}: {e}")
            return False
    
    def list_databases(self) -> List[Dict[str, str]]:
        """List all database containers managed by Launchbox"""
        try:
            containers = self.client.containers.list(
                all=True,
                filters={'label': 'launchbox.service=database'}
            )
            
            databases = []
            for container in containers:
                databases.append({
                    'name': container.name,
                    'app': container.labels.get('launchbox.app', 'unknown'),
                    'type': container.labels.get('launchbox.db_type', 'unknown'),
                    'status': container.status,
                    'created': container.attrs['Created']
                })
            
            return databases
            
        except Exception as e:
            logger.error(f"Failed to list databases: {e}")
            return []