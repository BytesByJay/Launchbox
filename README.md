# Launchbox 🚀

**Launchbox** is a powerful, self-hosted, open-source alternative to Heroku. It lets you deploy web applications by simply pushing your code via Git. Under the hood, it uses Docker and Traefik to build and serve your app locally under `*.localhost` subdomains.

---

## ✨ Features

- 🚀 **Git-push-to-deploy** (like Heroku)
- 🐳 **Docker-based** containerized apps
- 🌐 **Traefik-powered** reverse proxy with automatic routing
- 🧩 **Multi-app support** with isolated containers
- ⚙️ **Advanced configuration** via `launchbox.yaml`
- 🔒 **HTTPS support** with automatic SSL certificates (mkcert)
- 🗄️ **Database provisioning** (PostgreSQL, MySQL, MongoDB)
- 📊 **Web dashboard** for application management
- 🔧 **Resource limits** and health checks
- 📝 **Comprehensive logging** and error handling
- 🌍 **Environment variables** support (.env files)
- 🛠 **Zero DevOps setup** - works out of the box
- 📴 **Fully offline/local-friendly**

---

## 🚀 Quick Start

### Prerequisites
- Docker and Docker Compose
- Python 3.8+
- Git

### 1. Clone & Setup
```bash
git clone <repository-url> Launchbox
cd Launchbox

# Run setup script (creates directories, sets permissions, installs dependencies)
./setup.sh

# Start Traefik reverse proxy
docker-compose up -d
```

### 2. Update `/etc/hosts`
Add entries for local development:
```bash
echo "127.0.0.1 blog_app.localhost chat_app.localhost traefik.localhost dashboard.localhost" | sudo tee -a /etc/hosts
```

### 3. Start Web Dashboard (Optional)
```bash
# Run the dashboard
python3 -m launchbox.dashboard

# Access at http://localhost:8000
```

### 4. Create Your First App
```bash
# Create app directory
mkdir apps/blog_app && cd apps/blog_app
git init
```

Create `app.py`:
```python
from flask import Flask
import os

app = Flask(__name__)

@app.route("/")
def home():
    return "📝 Hello from Blog App!"

@app.route("/health")
def health():
    return {"status": "healthy"}

if __name__ == "__main__":
    port = int(os.environ.get('PORT', 3000))
    app.run(host="0.0.0.0", port=port)
```

Create `Dockerfile`:
```dockerfile
FROM python:3.11-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
EXPOSE 3000
CMD ["python", "app.py"]
```

Create `requirements.txt`:
```
flask==3.0.0
```

### 5. Configure Your App (Optional)
Create `launchbox.yaml` for advanced configuration:
```yaml
app:
  port: 3000
  health_check:
    path: "/health"
    interval: "30s"
    timeout: "10s"
    retries: 3

resources:
  memory: "256m"
  cpu: 0.5

environment:
  NODE_ENV: production
  DEBUG: false

database:
  enabled: false  # Set to true to auto-provision a database
  type: postgresql
  version: "13"

https:
  enabled: false  # Set to true for HTTPS
```

### 6. Deploy Your App
```bash
# Initialize Git repository for the app
cd ../../  # Back to Launchbox root
python3 -m launchbox.init blog_app

# Back to your app folder
cd apps/blog_app
git remote add launchbox ../../repos/blog_app.git
git add .
git commit -m "Initial deployment"
git push launchbox master
```

### 7. Access Your App
Open your browser and visit:
- **App**: http://blog_app.localhost
- **Dashboard**: http://localhost:8000
- **Traefik Dashboard**: http://traefik.localhost

---

## 📺 Project Structure
```
Launchbox/
├── docker-compose.yml           # Traefik configuration
├── launchbox/                   # Core Launchbox modules
│   ├── builder.py              # Docker image building
│   ├── runner.py               # Container deployment
│   ├── init.py                 # Git repository initialization
│   ├── config_parser.py        # Configuration management
│   ├── database_manager.py     # Database provisioning
│   ├── ssl_manager.py          # HTTPS/SSL management
│   ├── dashboard.py            # Web dashboard
│   ├── logger.py               # Logging utilities
│   └── templates/              # Dashboard HTML templates
├── apps/                        # Your applications
│   ├── blog_app/               # Example Flask app
│   └── chat_app/               # Another app
├── repos/                       # Git repositories (bare)
│   ├── blog_app.git/
│   └── chat_app.git/
├── traefik/                     # Traefik dynamic config
├── certs/                       # SSL certificates
└── logs/                        # Application logs
```

---

## ⚙️ Advanced Configuration

### Application Configuration (`launchbox.yaml`)

Each app can have a `launchbox.yaml` file for advanced configuration:

```yaml
# Application settings
app:
  port: 3000                    # Port your app runs on
  health_check:                 # Health check configuration
    path: "/health"
    interval: "30s"
    timeout: "10s"
    retries: 3
  build:                        # Build settings
    dockerfile: "Dockerfile"
    context: "."

# Resource limits
resources:
  memory: "512m"                # Memory limit (e.g., "256m", "1g")
  cpu: 0.5                      # CPU limit (0.5 = half a core)

# Environment variables
environment:
  NODE_ENV: production
  DEBUG: false
  API_KEY: "your-api-key"

# Database configuration
database:
  enabled: true                 # Enable database provisioning
  type: postgresql              # postgresql, mysql, mongodb
  version: "13"                 # Database version
  name: myapp_db               # Database name (optional)

# HTTPS configuration
https:
  enabled: true                 # Enable HTTPS
  redirect_http: true           # Redirect HTTP to HTTPS
```

### Environment Variables

Launchbox supports environment variables in multiple ways:

1. **launchbox.yaml**: Define in the `environment` section
2. **.env file**: Create a `.env` file in your app directory
3. **Database auto-injection**: Database connection info is automatically injected

#### Example `.env` file:
```env
DATABASE_URL=postgresql://user:pass@host:port/db
REDIS_URL=redis://localhost:6379
SECRET_KEY=your-secret-key
```

### Database Support

Launchbox can automatically provision databases for your applications:

#### Supported Databases:
- **PostgreSQL** (recommended)
- **MySQL**
- **MongoDB**

#### Auto-injected Environment Variables:
- `DATABASE_URL`: Full connection string
- `DB_HOST`: Database host
- `DB_PORT`: Database port
- `DB_NAME`: Database name
- `DB_USER`: Database user
- `DB_PASSWORD`: Database password
- `DB_TYPE`: Database type

### HTTPS/SSL Support

Launchbox supports local HTTPS using mkcert:

```bash
# Install mkcert (if not already installed)
# macOS: brew install mkcert
# Linux: See https://github.com/FiloSottile/mkcert

# Setup HTTPS for an app
# Add to launchbox.yaml:
https:
  enabled: true
  redirect_http: true
```

### Resource Management

Control container resources:

```yaml
resources:
  memory: "1g"        # Memory limit
  cpu: 1.5            # CPU limit (cores)
```

### Health Checks

Configure application health monitoring:

```yaml
app:
  health_check:
    path: "/health"      # Health check endpoint
    interval: "30s"     # Check interval
    timeout: "10s"      # Request timeout
    retries: 3          # Retry attempts
```

---

---

## 📊 Web Dashboard

Launchbox includes a web-based dashboard for managing your applications:

### Starting the Dashboard
```bash
python3 -m launchbox.dashboard
# Access at http://localhost:8000
```

### Dashboard Features
- 📊 **Overview**: System status and application metrics
- 🚀 **One-click deployment**: Deploy apps directly from the UI
- ▶️ **Container management**: Start/stop/restart applications
- 📝 **Log viewer**: Real-time application logs
- ⚙️ **Configuration viewer**: See app settings and environment
- 🔗 **Quick links**: Direct access to running applications

---

## 💻 CLI Usage

### Application Management
```bash
# Initialize a new app repository
python3 -m launchbox.init <app_name>

# Build an application
python3 -m launchbox.builder <app_name>

# Deploy an application
python3 -m launchbox.runner <app_name>

# Start the web dashboard
python3 -m launchbox.dashboard
```

### Git Commands
```bash
# Add Launchbox as remote (from your app directory)
git remote add launchbox ../../../repos/<app_name>.git

# Deploy your app
git push launchbox master

# View deployment logs
tail -f logs/builder.log logs/runner.log
```

---

## 🔁 How It Works

1. **Push Code**: You push code to `repos/app_name.git`
2. **Git Hook Triggers**: A `post-receive` hook automatically runs:
   - `builder.py` builds a Docker image from your code
   - `runner.py` deploys the container with proper configuration
   - Database containers are created if needed
   - SSL certificates are generated if HTTPS is enabled
3. **Automatic Routing**: Traefik automatically routes `app_name.localhost` to your container
4. **Health Monitoring**: Optional health checks ensure your app stays healthy

### Deployment Flow
```
Git Push → Post-receive Hook → Build Image → Setup Database → Deploy Container → Configure Routing
```

---

## 🔧 Troubleshooting

### Common Issues

#### Docker Connection Issues
```bash
# Ensure Docker is running
docker ps

# Check if Traefik network exists
docker network ls | grep traefik

# Restart Traefik if needed
docker-compose down && docker-compose up -d
```

#### Application Not Accessible
```bash
# Check if app container is running
docker ps | grep <app_name>

# Check container logs
docker logs <app_name>

# Verify /etc/hosts entry
grep localhost /etc/hosts

# Test direct container access
curl http://localhost:<port> # if port is exposed
```

#### Build Failures
```bash
# Check build logs
tail -f logs/builder.log

# Verify Dockerfile exists and is valid
cd apps/<app_name> && docker build -t test .

# Check disk space
df -h
```

#### Database Connection Issues
```bash
# Check database container status
docker ps | grep <app_name>_postgres

# Check database logs
docker logs <app_name>_postgres

# Test database connection
docker exec -it <app_name>_postgres psql -U launchbox -d <db_name>
```

### Debugging Tips

1. **Enable Debug Logging**: Set environment variable `LAUNCHBOX_LOG_LEVEL=DEBUG`
2. **Check All Logs**: Monitor `logs/` directory for detailed logs
3. **Inspect Containers**: Use `docker inspect <container>` for detailed info
4. **Network Issues**: Verify all containers are on `traefik_default` network
5. **Port Conflicts**: Ensure ports 80, 443, and 8080 are available

---

## 🧠 Why Use Launchbox?

- 💰 **Cost-effective**: No cloud deployment fees
- 🚀 **Heroku-like Experience**: Familiar git-push workflow
- 💻 **Local Development**: Perfect for prototyping and testing
- 🔒 **Privacy**: Your code never leaves your machine
- 🎨 **Customizable**: Full control over your deployment pipeline
- 📚 **Educational**: Learn Docker, Git hooks, Traefik, and DevOps
- 🔧 **Extensible**: Easy to modify and extend for your needs

---

## 🛠 Roadmap

### ✅ Completed
- [x] HTTPS with mkcert integration
- [x] App config via `launchbox.yaml`
- [x] Web dashboard with full management UI
- [x] Database auto-provisioning (PostgreSQL, MySQL, MongoDB)
- [x] Environment variables support (.env files)
- [x] Resource limits and health checks
- [x] Comprehensive logging and error handling
- [x] SSL certificate management

### 📝 Planned Features
- [ ] **Remote deployment** (deploy to VPS/cloud)
- [ ] **Multi-environment support** (dev/staging/prod)
- [ ] **Database migrations** management
- [ ] **Redis/caching** support
- [ ] **Load balancer** configuration
- [ ] **Backup and restore** functionality
- [ ] **CI/CD integration** (GitHub Actions, GitLab CI)
- [ ] **Metrics and monitoring** (Prometheus/Grafana)
- [ ] **Custom domains** support
- [ ] **App scaling** (multiple container instances)

---

## 🧪 License
MIT. Fork it. Hack it. Deploy everything with a push.

---

## 🙌 Credits
Inspired by Heroku, Dokku, Railway, and good old `git push && go` workflows.
