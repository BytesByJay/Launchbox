# Launchbox

**Self-hosted PaaS — Heroku-like deployments on your own machine**

Launchbox is an open-source, lightweight platform that brings the "git push to deploy" experience to your local environment (or small server).  
Built on **Docker** + **Traefik**, it runs apps under `*.localhost` domains with zero cloud dependency.

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)
[![Python](https://img.shields.io/badge/python-3.8%2B-blue)](https://www.python.org/)
![Docker](https://img.shields.io/badge/docker-%230db7ed.svg?style=flat&logo=docker&logoColor=white)

## Features

- Git-push-to-deploy workflow (Heroku style)
- Automatic Docker image building & container orchestration
- Traefik reverse proxy → instant `app-name.localhost` routing
- Optional HTTPS with mkcert (local trusted certificates)
- Automatic provisioning of PostgreSQL, MySQL or MongoDB
- Resource limits (CPU/memory), health checks & auto-restarts
- `.env` + `launchbox.yaml` configuration
- Built-in web dashboard for monitoring & management
- Comprehensive logging (per-app + system)
- Fully offline / local-first — no internet required after setup

## Quick Start

### Prerequisites

- Docker & Docker Compose
- Python 3.8+
- Git
- (Optional) mkcert for local HTTPS

### Installation

```bash
git clone https://github.com/yourusername/launchbox.git
cd launchbox

# Run initial setup (creates dirs, sets permissions, etc.)
./setup.sh

# Start Traefik
docker compose up -d
```

Add wildcard domain to `/etc/hosts` (or equivalent):

```bash
sudo tee -a /etc/hosts <<EOF
127.0.0.1 traefik.localhost dashboard.localhost
EOF
```

### Deploy Your First App

1. Create app folder

```bash
mkdir -p apps/myapp && cd apps/myapp
git init
```

2. Add minimal Flask example (or any Dockerfile-based app)

`app.py`
```python
from flask import Flask
app = Flask(__name__)

@app.route("/")
def hello():
    return "Hello from Launchbox!"

if __name__ == "__main__":
    import os
    port = int(os.getenv("PORT", 8000))
    app.run(host="0.0.0.0", port=port)
```

`Dockerfile`
```dockerfile
FROM python:3.11-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
EXPOSE 8000
CMD ["python", "app.py"]
```

`requirements.txt`
```
flask==3.0.3
```

3. Deploy

```bash
# From project root
python -m launchbox.init myapp

# From app folder
cd ../..
git remote add launchbox ../repos/myapp.git
git add . && git commit -m "Initial commit"
git push launchbox main
```

→ Visit http://myapp.localhost

## Table of Contents

- [Features](#features)
- [Quick Start](#quick-start)
- [Configuration](#configuration)
- [Web Dashboard](#web-dashboard)
- [CLI Commands](#cli-commands)
- [How It Works](#how-it-works)
- [Troubleshooting](#troubleshooting)
- [Why Launchbox?](#why-launchbox)
- [Roadmap](#roadmap)
- [License](#license)

## Configuration

Per-app settings live in `apps/<app-name>/launchbox.yaml`

```yaml
app:
  port: 8000
  health_check:
    path: /health
    interval: 30s
    timeout: 10s
    retries: 3

resources:
  cpu: 0.5
  memory: 512Mi

environment:
  DEBUG: false
  API_KEY: "${SECRET_API_KEY}"

database:
  enabled: true
  type: postgresql
  version: "16"

https:
  enabled: true
  redirect_http: true
```

Also supports `.env` files in the app root.

## Web Dashboard

```bash
python -m launchbox.dashboard
```

→ http://localhost:8000 (or http://dashboard.localhost)

Features:

- Application list & status
- Start / stop / restart containers
- Real-time logs
- Configuration viewer
- Quick links to apps & Traefik dashboard (http://traefik.localhost)

## CLI Commands

```bash
python -m launchbox.init <app-name>       # Create git bare repo
python -m launchbox.builder <app-name>    # Manual build
python -m launchbox.runner <app-name>     # Manual deploy
python -m launchbox.dashboard             # Start UI
```

## How It Works

```
git push → post-receive hook
         → builder.py (docker build)
         → runner.py (docker run + traefik labels)
         → database provisioning (if enabled)
         → Traefik routes *.localhost → container
```

## Troubleshooting

- App not reachable? → `docker ps`, `docker logs <container>`, check `/etc/hosts`
- Build fails? → `tail -f logs/builder.log`
- Traefik issues? → `docker compose restart`
- Debug mode: `export LAUNCHBOX_LOG_LEVEL=DEBUG`

Full guide → [Troubleshooting](./docs/troubleshooting.md) (add later)

## Why Launchbox?

- **Zero cloud cost** — run everything locally or on a cheap VPS
- **Heroku-like simplicity** without vendor lock-in
- **Privacy-first** — your code & data never leave your control
- **Great for learning** Docker, Traefik, Git hooks & local PaaS concepts
- **Fully extensible** — MIT licensed, hack away

## Roadmap

- [x] Local HTTPS (mkcert)
- [x] Web dashboard
- [x] Database provisioning
- [x] Resource constraints & health checks
- [ ] Remote server deployment (single VPS / swarm)
- [ ] Multi-environment (dev/stage/prod)
- [ ] One-click templates
- [ ] Basic metrics (Prometheus export)
- [ ] App scaling (replicas)

## License

MIT

Inspired by Dokku, Coolify, CapRover, Heroku buildpacks philosophy.

Happy pushing! 
