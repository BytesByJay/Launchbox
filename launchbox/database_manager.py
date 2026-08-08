import docker
import secrets
import subprocess
import time
from typing import Dict, Optional, List, Tuple
from launchbox.logger import setup_logger, LaunchboxError
from launchbox.config_parser import LaunchboxConfig
from launchbox.state import StateStore

logger = setup_logger("database_manager")

# The username is shared across applications on purpose. Every application
# gets its OWN database container, so it is already an isolated database
# server -- two applications sharing the login name grants neither any
# access to the other. The password is what carries the isolation, and that
# is generated per application. A per-application username would also risk
# MySQL's 32-character username limit for longer application names.
DB_USERNAME = 'launchbox'

# What every database provisioned before per-application credentials used.
# A container's password is written into its data directory the first time
# it boots and cannot be changed by passing a different environment
# variable afterwards, so a pre-existing container must keep being handed
# this pair -- generating a new one would simply lock the application out
# of its own database.
LEGACY_PASSWORD = 'launchbox123'

# token_urlsafe emits only [A-Za-z0-9_-], every character of which is safe
# in the userinfo section of a connection URI. A generator that could emit
# ':', '@' or '/' would silently corrupt DATABASE_URL for some passwords
# and not others.
PASSWORD_BYTES = 24

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
    
    def create_database_for_app(
        self,
        app_name: str,
        app_path: str,
        config: Optional[LaunchboxConfig] = None,
    ) -> Optional[Dict[str, str]]:
        """Create database container for application if needed.

        ``config`` lets a caller that has already resolved the configuration
        hand it in rather than have it re-read from ``app_path``. A rollback
        replays a configuration recorded on the deployment row and has no
        source directory to read, so without this it would provision against
        built-in defaults (postgresql/13) regardless of what the deployment
        being restored actually used.
        """
        if config is None:
            config = LaunchboxConfig(app_path)
        
        if not config.is_database_enabled():
            logger.debug(f"Database not enabled for {app_name}")
            return None
        
        db_config = config.get_database_config()
        db_type = db_config.get('type', 'postgresql')
        db_version = db_config.get('version', '13')
        db_name = db_config.get('name', f"{app_name}_db")
        
        logger.info(f"Setting up {db_type} database for {app_name}")

        container_names = {
            'postgresql': f"{app_name}_postgres",
            'mysql': f"{app_name}_mysql",
            'mongodb': f"{app_name}_mongodb",
        }
        if db_type not in container_names:
            raise DatabaseError(f"Unsupported database type: {db_type}")

        username, password = self._resolve_credentials(
            app_name, db_type, container_names[db_type]
        )

        if db_type == 'postgresql':
            connection_info = self._create_postgresql(
                app_name, db_name, db_version, username, password)
        elif db_type == 'mysql':
            connection_info = self._create_mysql(
                app_name, db_name, db_version, username, password)
        else:
            connection_info = self._create_mongodb(
                app_name, db_name, db_version, username, password)

        self._record_state(app_name, db_type, connection_info)
        return connection_info

    def _container_exists(self, container_name: str) -> bool:
        try:
            self.client.containers.get(container_name)
            return True
        except docker.errors.NotFound:
            return False
        except Exception as e:
            # An unreachable daemon is not evidence that the container is
            # absent. Treating it as absent would generate a password for a
            # container that already exists with a different one.
            logger.warning(f"Could not check for {container_name}: {e}")
            raise DatabaseError(f"Could not check for {container_name}: {e}")

    def _resolve_credentials(
        self, app_name: str, engine: str, container_name: str
    ) -> Tuple[str, str]:
        """The username and password this application's database uses.

        Three cases, in order:

        1. Credentials already recorded for this exact container -- reuse
           them. Provisioning runs on every deployment and must be
           idempotent; regenerating here would hand the application a
           password its database does not have.
        2. No record, but the container exists -- it predates per-application
           credentials. Its password lives in its data directory and cannot
           be changed by passing a new environment variable, so adopt the
           historical fixed pair and record it.
        3. Nothing exists yet -- generate a fresh password.
        """
        try:
            with StateStore() as store:
                row = store.get_database(app_name)
        except Exception as e:
            logger.warning(f"Could not read stored credentials for {app_name}: {e}")
            row = None

        if (row and row['engine'] == engine
                and row['container_name'] == container_name
                and row['password']):
            logger.debug(f"Reusing stored credentials for {container_name}")
            return row['username'], row['password']

        if self._container_exists(container_name):
            logger.info(
                f"Adopting pre-existing credentials for {container_name}: it "
                "was provisioned before per-application passwords and its "
                "password is fixed in its data directory"
            )
            return DB_USERNAME, LEGACY_PASSWORD

        logger.info(f"Generating a new password for {container_name}")
        return DB_USERNAME, secrets.token_urlsafe(PASSWORD_BYTES)

    def _record_state(
        self, app_name: str, db_type: str, connection_info: Dict[str, str]
    ) -> None:
        """Make a provisioned database visible to the dashboard and CLI.

        Every engine's connection_info carries the same DB_HOST/DB_NAME/
        DB_USER/DB_PASSWORD shape, so this is written once here rather than
        duplicated at each engine's several return points. Provisioning
        itself must never fail because this bookkeeping step did -- a state
        store problem is logged and swallowed, not raised.

        ``volume_name`` is recorded as empty: no engine mounts a named
        volume today, so a container's data does not survive `docker rm`.
        The state schema requires a value; an honest empty string, not a
        volume name that would imply persistence that does not exist.
        """
        try:
            with StateStore() as store:
                store.record_database(
                    app_name,
                    db_type,
                    connection_info.get('DB_HOST', ''),
                    '',
                    connection_info.get('DB_NAME', ''),
                    connection_info.get('DB_USER', ''),
                    connection_info.get('DB_PASSWORD', ''),
                )
        except Exception as e:
            logger.warning(f"Failed to record database state for {app_name}: {e}")
    
    def _create_postgresql(self, app_name: str, db_name: str, version: str,
                           username: str, password: str) -> Dict[str, str]:
        """Create PostgreSQL container"""
        container_name = f"{app_name}_postgres"
        
        try:
            # Check if container already exists
            try:
                container = self.client.containers.get(container_name)
                if container.status == 'running':
                    logger.info(f"PostgreSQL container {container_name} already running")
                    return self._get_postgres_connection_info(
                        container_name, db_name, username, password)
                else:
                    container.start()
                    logger.info(f"Started existing PostgreSQL container {container_name}")
                    return self._get_postgres_connection_info(
                        container_name, db_name, username, password)
            except docker.errors.NotFound:
                pass
            
            # Create new container
            logger.info(f"Creating PostgreSQL container: {container_name}")
            
            # POSTGRES_HOST_AUTH_METHOD=trust was set here previously, which
            # tells Postgres to accept every connection without checking a
            # password at all -- it would make the generated password below
            # decorative. Removed so the credentials are actually enforced.
            environment = {
                'POSTGRES_DB': db_name,
                'POSTGRES_USER': username,
                'POSTGRES_PASSWORD': password,
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
            self._wait_for_postgres(container_name, username=username)
            
            logger.info(f"PostgreSQL container {container_name} created and ready")
            return self._get_postgres_connection_info(
                container_name, db_name, username, password)
            
        except Exception as e:
            logger.error(f"Failed to create PostgreSQL container: {e}")
            raise DatabaseError(f"PostgreSQL setup failed: {e}")
    
    def _create_mysql(self, app_name: str, db_name: str, version: str,
                      username: str, password: str) -> Dict[str, str]:
        """Create MySQL container"""
        container_name = f"{app_name}_mysql"
        
        try:
            # Check if container already exists
            try:
                container = self.client.containers.get(container_name)
                if container.status == 'running':
                    logger.info(f"MySQL container {container_name} already running")
                    return self._get_mysql_connection_info(
                        container_name, db_name, username, password)
                else:
                    container.start()
                    logger.info(f"Started existing MySQL container {container_name}")
                    return self._get_mysql_connection_info(
                        container_name, db_name, username, password)
            except docker.errors.NotFound:
                pass
            
            # Create new container
            logger.info(f"Creating MySQL container: {container_name}")
            
            # The root password is generated and deliberately not stored:
            # nothing in Launchbox connects as root, and a known constant
            # root password on every MySQL container is exactly the hazard
            # this change exists to remove.
            environment = {
                'MYSQL_DATABASE': db_name,
                'MYSQL_USER': username,
                'MYSQL_PASSWORD': password,
                'MYSQL_ROOT_PASSWORD': secrets.token_urlsafe(PASSWORD_BYTES),
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
            self._wait_for_mysql(container_name, username=username,
                                 password=password)
            
            logger.info(f"MySQL container {container_name} created and ready")
            return self._get_mysql_connection_info(
                container_name, db_name, username, password)
            
        except Exception as e:
            logger.error(f"Failed to create MySQL container: {e}")
            raise DatabaseError(f"MySQL setup failed: {e}")
    
    def _create_mongodb(self, app_name: str, db_name: str, version: str,
                        username: str, password: str) -> Dict[str, str]:
        """Create MongoDB container"""
        container_name = f"{app_name}_mongodb"
        
        try:
            # Check if container already exists
            try:
                container = self.client.containers.get(container_name)
                if container.status == 'running':
                    logger.info(f"MongoDB container {container_name} already running")
                    return self._get_mongodb_connection_info(
                        container_name, db_name, username, password)
                else:
                    container.start()
                    logger.info(f"Started existing MongoDB container {container_name}")
                    return self._get_mongodb_connection_info(
                        container_name, db_name, username, password)
            except docker.errors.NotFound:
                pass
            
            # Create new container
            logger.info(f"Creating MongoDB container: {container_name}")
            
            environment = {
                'MONGO_INITDB_DATABASE': db_name,
                'MONGO_INITDB_ROOT_USERNAME': username,
                'MONGO_INITDB_ROOT_PASSWORD': password,
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
            return self._get_mongodb_connection_info(
                container_name, db_name, username, password)
            
        except Exception as e:
            logger.error(f"Failed to create MongoDB container: {e}")
            raise DatabaseError(f"MongoDB setup failed: {e}")
    
    def _wait_for_postgres(self, container_name: str, timeout: int = 30,
                           username: str = DB_USERNAME):
        """Wait for PostgreSQL to be ready"""
        logger.info(f"Waiting for PostgreSQL {container_name} to be ready...")
        
        for i in range(timeout):
            try:
                result = self.client.containers.get(container_name).exec_run(
                    f"pg_isready -U {username}"
                )
                if result.exit_code == 0:
                    logger.info(f"PostgreSQL {container_name} is ready")
                    return
            except Exception:
                pass
            
            time.sleep(1)
        
        raise DatabaseError(f"PostgreSQL {container_name} failed to become ready within {timeout} seconds")
    
    def _wait_for_mysql(self, container_name: str, timeout: int = 30,
                        username: str = DB_USERNAME,
                        password: str = LEGACY_PASSWORD):
        """Wait for MySQL to be ready"""
        logger.info(f"Waiting for MySQL {container_name} to be ready...")
        
        for i in range(timeout):
            try:
                result = self.client.containers.get(container_name).exec_run(
                    ["mysqladmin", "ping", "-h", "localhost",
                     "-u", username, f"-p{password}"]
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
    
    def _get_postgres_connection_info(self, container_name: str, db_name: str,
                                      username: str, password: str) -> Dict[str, str]:
        """Get PostgreSQL connection information"""
        return {
            'DATABASE_URL': f"postgresql://{username}:{password}@{container_name}:5432/{db_name}",
            'DB_HOST': container_name,
            'DB_PORT': '5432',
            'DB_NAME': db_name,
            'DB_USER': username,
            'DB_PASSWORD': password,
            'DB_TYPE': 'postgresql'
        }
    
    def _get_mysql_connection_info(self, container_name: str, db_name: str,
                                   username: str, password: str) -> Dict[str, str]:
        """Get MySQL connection information"""
        return {
            'DATABASE_URL': f"mysql://{username}:{password}@{container_name}:3306/{db_name}",
            'DB_HOST': container_name,
            'DB_PORT': '3306',
            'DB_NAME': db_name,
            'DB_USER': username,
            'DB_PASSWORD': password,
            'DB_TYPE': 'mysql'
        }
    
    def _get_mongodb_connection_info(self, container_name: str, db_name: str,
                                     username: str, password: str) -> Dict[str, str]:
        """Get MongoDB connection information"""
        return {
            'DATABASE_URL': f"mongodb://{username}:{password}@{container_name}:27017/{db_name}",
            'DB_HOST': container_name,
            'DB_PORT': '27017',
            'DB_NAME': db_name,
            'DB_USER': username,
            'DB_PASSWORD': password,
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