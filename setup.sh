#!/bin/bash

# Launchbox Setup Script
# This script ensures all necessary directories exist with proper permissions

set -e

echo "🚀 Setting up Launchbox..."

# Create necessary directories
echo "📁 Creating directories..."
mkdir -p apps repos letsencrypt certs traefik logs

# Set proper permissions
echo "🔧 Setting permissions..."
chmod 755 apps repos certs traefik logs
chmod 700 letsencrypt

# Create acme.json if it doesn't exist
if [ ! -f letsencrypt/acme.json ]; then
    echo "📄 Creating acme.json..."
    touch letsencrypt/acme.json
    chmod 600 letsencrypt/acme.json
fi

# Install Python dependencies
if [ -f requirements.txt ]; then
    echo "🐍 Installing Python dependencies..."
    pip3 install --user -r requirements.txt
fi

echo "✅ Launchbox setup complete!"
echo ""
echo "Next steps:"
echo "1. Start Traefik: docker-compose up -d"
echo "2. Start dashboard: python3 -m launchbox.dashboard"
echo "3. Create your first app in the apps/ directory"
echo ""
echo "Visit http://localhost:8000 for the web dashboard"