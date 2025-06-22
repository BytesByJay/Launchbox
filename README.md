# Launchbox 🚀

**Launchbox** is a minimal, self-hosted, open-source alternative to Heroku. It lets you deploy web applications by simply pushing your code via Git. Under the hood, it uses Docker and Traefik to build and serve your app locally under `*.localhost` subdomains.

---

## ✨ Features

- ✅ Git-push-to-deploy (like Heroku)
- 🐳 Docker-based containerized apps
- 🌐 Traefik-powered reverse proxy
- 🧩 Supports multiple apps
- 🛠 Zero DevOps setup
- ⚙️ Fully offline/local-friendly

---

## 🚀 Quick Start

### 1. Clone & Run Traefik
```bash
cd Launchbox/Launchbox
docker-compose up -d
```

### 2. Update `/etc/hosts`
Add entries like:
```
127.0.0.1 blog_app.localhost chat_app.localhost traefik.localhost
```

### 3. Create and Deploy an App
```bash
cd ~/projects/Launchbox
mkdir blog_app && cd blog_app
git init
```

Add this to `app.py`:
```python
from flask import Flask
app = Flask(__name__)
@app.route("/")
def home():
    return "📝 Hello from Blog App!"
app.run(host="0.0.0.0", port=3000)
```

Add a `Dockerfile`:
```Dockerfile
FROM python:3.10-slim
WORKDIR /app
COPY . .
RUN pip install flask
CMD ["python", "app.py"]
```

### 4. Set up Git Remote
```bash
# From Launchbox directory
python3 -m launchbox.init blog_app

# Back in your app folder
git remote add launchbox ../Launchbox/Launchbox/repos/blog_app.git
git add .
git commit -m "Deploy blog app"
git push launchbox master
```

Open in browser:
```
http://blog_app.localhost
```

---

## 📦 Repo Structure
```
Launchbox/
├── Launchbox/
│   ├── docker-compose.yml
│   ├── launchbox/
│   │   ├── builder.py
│   │   ├── runner.py
│   │   └── init.py
│   └── repos/
├── blog_app/        # your Flask or Node app
├── chat_app/        # another app
```

---

## 🔁 How It Works
1. You push code to `repos/app_name.git`
2. A Git `post-receive` hook runs:
   - `builder.py` to Docker build the app
   - `runner.py` to Docker run it
3. Traefik auto-routes `app_name.localhost` to the container

---

## 🧠 Why Use Launchbox?
- Tired of cloud deploy fees
- Want local Heroku-style deployment
- Build many small apps
- Learn Docker, Git hooks, Traefik, and DevOps

---

## 🛠 Roadmap / Ideas
- [ ] HTTPS with mkcert
- [ ] App config via `launchbox.yaml`
- [ ] Remote deployment (to VPS)
- [ ] Web dashboard

---

## 🧪 License
MIT. Fork it. Hack it. Deploy everything with a push.

---

## 🙌 Credits
Inspired by Heroku, Dokku, Railway, and good old `git push && go` workflows.
