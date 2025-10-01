#!/bin/bash

# Launchbox Production Deployment Script
# This script sets up Launchbox for production use

set -e

echo "🚀 Deploying Launchbox for Production..."

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
BLUE='\033[0;34m'
YELLOW='\033[1;33m'
NC='\033[0m' # No Color

# Configuration
LAUNCHBOX_DIR="/opt/launchbox"
LAUNCHBOX_USER="launchbox"
SERVICE_NAME="launchbox-dashboard"

# Check if running as root
if [[ $EUID -eq 0 ]]; then
   echo -e "${RED}This script should not be run as root${NC}"
   echo "Please run as a regular user with sudo privileges"
   exit 1
fi

# Function to print colored output
print_status() {
    echo -e "${BLUE}[INFO]${NC} $1"
}

print_success() {
    echo -e "${GREEN}[SUCCESS]${NC} $1"
}

print_warning() {
    echo -e "${YELLOW}[WARNING]${NC} $1"
}

print_error() {
    echo -e "${RED}[ERROR]${NC} $1"
}

# Check prerequisites
print_status "Checking prerequisites..."

# Check Docker
if ! command -v docker &> /dev/null; then
    print_error "Docker is not installed. Please install Docker first."
    exit 1
fi

# Check Docker Compose
if ! docker compose version &> /dev/null; then
    print_error "Docker Compose is not available. Please install Docker Compose."
    exit 1
fi

# Check Python 3
if ! command -v python3 &> /dev/null; then
    print_error "Python 3 is not installed. Please install Python 3."
    exit 1
fi

print_success "All prerequisites are satisfied"

# Create production directory
print_status "Setting up production directory..."
sudo mkdir -p $LAUNCHBOX_DIR
sudo chown $USER:$USER $LAUNCHBOX_DIR

# Copy Launchbox files
print_status "Copying Launchbox files..."
cp -r ./* $LAUNCHBOX_DIR/
cd $LAUNCHBOX_DIR

# Set proper permissions
print_status "Setting up permissions..."
chmod +x setup.sh deploy-production.sh
sudo mkdir -p /var/log/launchbox
sudo chown $USER:$USER /var/log/launchbox

# Install Python dependencies
print_status "Installing Python dependencies..."
pip3 install --user -r requirements.txt

# Run setup script
print_status "Running Launchbox setup..."
./setup.sh

# Create systemd service for dashboard
print_status "Creating systemd service..."
sudo tee /etc/systemd/system/$SERVICE_NAME.service > /dev/null <<EOF
[Unit]
Description=Launchbox Dashboard
After=network.target docker.service
Requires=docker.service

[Service]
Type=simple
User=$USER
Group=$USER
WorkingDirectory=$LAUNCHBOX_DIR
Environment=PYTHONPATH=$LAUNCHBOX_DIR
ExecStart=/usr/bin/python3 -m launchbox.dashboard
Restart=always
RestartSec=10
StandardOutput=append:/var/log/launchbox/dashboard.log
StandardError=append:/var/log/launchbox/dashboard.log

[Install]
WantedBy=multi-user.target
EOF

# Start and enable services
print_status "Starting services..."
sudo systemctl daemon-reload
sudo systemctl enable $SERVICE_NAME

# Start Traefik
print_status "Starting Traefik..."
docker compose up -d

# Start dashboard
print_status "Starting Launchbox dashboard..."
sudo systemctl start $SERVICE_NAME

# Configure firewall (if firewalld is running)
if systemctl is-active --quiet firewalld; then
    print_status "Configuring firewall..."
    sudo firewall-cmd --permanent --add-port=80/tcp
    sudo firewall-cmd --permanent --add-port=443/tcp
    sudo firewall-cmd --permanent --add-port=8000/tcp
    sudo firewall-cmd --permanent --add-port=8080/tcp
    sudo firewall-cmd --reload
fi

# Create convenience scripts
print_status "Creating convenience scripts..."

# Launchbox CLI wrapper
sudo tee /usr/local/bin/launchbox > /dev/null <<EOF
#!/bin/bash
cd $LAUNCHBOX_DIR
export PYTHONPATH=$LAUNCHBOX_DIR

case "\$1" in
    "init")
        python3 -m launchbox.init "\$2"
        ;;
    "build")
        python3 -m launchbox.builder "\$2"
        ;;
    "deploy")
        python3 -m launchbox.runner "\$2"
        ;;
    "dashboard")
        echo "Dashboard is running as a service at http://localhost:8000"
        echo "Use 'sudo systemctl status $SERVICE_NAME' to check status"
        ;;
    "status")
        sudo systemctl status $SERVICE_NAME
        docker compose ps
        docker ps --format "table {{.Names}}\t{{.Status}}\t{{.Ports}}"
        ;;
    "logs")
        if [ -z "\$2" ]; then
            echo "Usage: launchbox logs <app_name>"
            exit 1
        fi
        docker logs "\$2" -f
        ;;
    "restart")
        if [ -z "\$2" ]; then
            echo "Restarting all services..."
            docker compose restart
            sudo systemctl restart $SERVICE_NAME
        else
            echo "Restarting \$2..."
            docker restart "\$2"
        fi
        ;;
    *)
        echo "Launchbox CLI - Production PaaS Platform"
        echo ""
        echo "Usage: launchbox <command> [args]"
        echo ""
        echo "Commands:"
        echo "  init <app_name>     Initialize a new application repository"
        echo "  build <app_name>    Build application Docker image"
        echo "  deploy <app_name>   Deploy application"
        echo "  dashboard           Show dashboard information"
        echo "  status              Show status of all services"
        echo "  logs <app_name>     Show application logs"
        echo "  restart [app_name]  Restart service(s)"
        echo ""
        echo "Dashboard: http://localhost:8000"
        echo "Traefik:   http://localhost:8080"
        ;;
esac
EOF

sudo chmod +x /usr/local/bin/launchbox

# Final status check
print_status "Performing final status check..."
sleep 5

# Check if services are running
if sudo systemctl is-active --quiet $SERVICE_NAME; then
    print_success "Launchbox Dashboard is running"
else
    print_error "Launchbox Dashboard failed to start"
fi

if docker compose ps | grep -q "traefik.*Up"; then
    print_success "Traefik is running"
else
    print_error "Traefik failed to start"
fi

echo ""
echo "🎉 Launchbox Production Deployment Complete!"
echo ""
echo -e "${GREEN}Services:${NC}"
echo "  • Dashboard: http://localhost:8000"
echo "  • Traefik:   http://localhost:8080"
echo "  • Apps:      http://<app_name>.localhost"
echo ""
echo -e "${GREEN}Commands:${NC}"
echo "  • launchbox status          - Check all services"
echo "  • launchbox init <app>      - Initialize new app"
echo "  • launchbox deploy <app>    - Deploy application"
echo "  • launchbox logs <app>      - View app logs"
echo ""
echo -e "${GREEN}Logs:${NC}"
echo "  • Dashboard: /var/log/launchbox/dashboard.log"
echo "  • App logs:  /opt/launchbox/logs/"
echo ""
echo -e "${YELLOW}Next Steps:${NC}"
echo "  1. Add apps to /opt/launchbox/apps/"
echo "  2. Use 'launchbox init <app_name>' to set up deployment"
echo "  3. Deploy with git push to the created repositories"
echo ""