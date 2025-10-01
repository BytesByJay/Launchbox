from flask import Flask, jsonify
import os
import time

app = Flask(__name__)

@app.route("/")
def home():
    return """
    <h1>🚀 Demo App - Launchbox Deployment</h1>
    <p>This app demonstrates the complete Launchbox flow!</p>
    <ul>
        <li><a href="/health">Health Check</a></li>
        <li><a href="/env">Environment Variables</a></li>
        <li><a href="/info">App Info</a></li>
    </ul>
    """

@app.route("/health")
def health():
    return jsonify({
        "status": "healthy",
        "timestamp": time.time(),
        "app": "demo_app"
    })

@app.route("/env")
def env_vars():
    env_info = {}
    for key, value in os.environ.items():
        if key.startswith(('DB_', 'DATABASE_', 'PORT', 'NODE_ENV', 'DEBUG')):
            env_info[key] = value
    
    return jsonify({
        "environment_variables": env_info,
        "port": os.environ.get('PORT', '3000')
    })

@app.route("/info")
def app_info():
    return jsonify({
        "app_name": "demo_app",
        "version": "1.0.0",
        "framework": "Flask",
        "deployment_platform": "Launchbox",
        "port": int(os.environ.get('PORT', 3000)),
        "environment": os.environ.get('NODE_ENV', 'development')
    })

if __name__ == "__main__":
    port = int(os.environ.get('PORT', 3000))
    app.run(host="0.0.0.0", port=port, debug=True)