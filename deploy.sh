#!/bin/bash
# =============================================================================
# deploy.sh — One-shot EC2 setup for Campus Complaint & Issue Tracker
# Tested on: Amazon Linux 2 / Ubuntu 22.04
#
# Usage (run as ec2-user or ubuntu on your EC2 instance):
#   chmod +x deploy.sh
#   ./deploy.sh
# =============================================================================

set -e  # Exit immediately on any error

APP_DIR="/home/$(whoami)/campus-tracker"
SERVICE_NAME="campus-tracker"

echo "=========================================="
echo " Campus Tracker — EC2 Deployment Script"
echo "=========================================="

# --------------------------------------------------------------------------
# 1. System packages
# --------------------------------------------------------------------------
echo ""
echo "[1/7] Installing system packages..."

if command -v apt-get &>/dev/null; then
    # Ubuntu / Debian
    sudo apt-get update -y
    sudo apt-get install -y python3 python3-pip python3-venv nginx
elif command -v yum &>/dev/null; then
    # Amazon Linux 2
    sudo yum update -y
    sudo yum install -y python3 python3-pip nginx
fi

# --------------------------------------------------------------------------
# 2. Project directory
# --------------------------------------------------------------------------
echo ""
echo "[2/7] Setting up project directory at $APP_DIR ..."
mkdir -p "$APP_DIR"

# Copy all project files to APP_DIR (if running from project root)
cp -r . "$APP_DIR/" 2>/dev/null || true

cd "$APP_DIR"

# --------------------------------------------------------------------------
# 3. Python virtual environment + dependencies
# --------------------------------------------------------------------------
echo ""
echo "[3/7] Creating Python virtual environment and installing dependencies..."
python3 -m venv venv
source venv/bin/activate
pip install --upgrade pip
pip install -r requirements.txt

# --------------------------------------------------------------------------
# 4. Environment file
# --------------------------------------------------------------------------
echo ""
echo "[4/7] Setting up .env ..."
if [ ! -f "$APP_DIR/.env" ]; then
    cp "$APP_DIR/.env.example" "$APP_DIR/.env"
    echo ""
    echo "  *** ACTION REQUIRED ***"
    echo "  Created .env from .env.example."
    echo "  Edit $APP_DIR/.env and fill in:"
    echo "    - AWS_REGION"
    echo "    - DYNAMODB_TABLE"
    echo "    - S3_BUCKET"
    echo "    (Leave AWS_ACCESS_KEY_ID and AWS_SECRET_ACCESS_KEY BLANK if using IAM Role)"
    echo ""
    read -p "  Press [Enter] after editing .env to continue..."
else
    echo "  .env already exists — skipping."
fi

# --------------------------------------------------------------------------
# 5. Gunicorn log directory
# --------------------------------------------------------------------------
echo ""
echo "[5/7] Creating log directory..."
sudo mkdir -p /var/log/campus-tracker
sudo chown "$(whoami):$(whoami)" /var/log/campus-tracker

# --------------------------------------------------------------------------
# 6. systemd service
# --------------------------------------------------------------------------
echo ""
echo "[6/7] Installing systemd service..."

# Update WorkingDirectory and ExecStart paths in service file
sed -i "s|/home/ec2-user/campus-tracker|$APP_DIR|g" "$APP_DIR/campus-tracker.service"
sed -i "s|User=ec2-user|User=$(whoami)|g"           "$APP_DIR/campus-tracker.service"
sed -i "s|Group=ec2-user|Group=$(whoami)|g"         "$APP_DIR/campus-tracker.service"

sudo cp "$APP_DIR/campus-tracker.service" "/etc/systemd/system/${SERVICE_NAME}.service"
sudo systemctl daemon-reload
sudo systemctl enable "$SERVICE_NAME"
sudo systemctl restart "$SERVICE_NAME"

echo "  Service status:"
sudo systemctl status "$SERVICE_NAME" --no-pager || true

# --------------------------------------------------------------------------
# 7. Nginx
# --------------------------------------------------------------------------
echo ""
echo "[7/7] Configuring Nginx..."
sudo cp "$APP_DIR/nginx.conf" "/etc/nginx/conf.d/${SERVICE_NAME}.conf"

# Remove default nginx site if it exists
sudo rm -f /etc/nginx/sites-enabled/default 2>/dev/null || true

sudo nginx -t   # Test config
sudo systemctl enable nginx
sudo systemctl restart nginx

# --------------------------------------------------------------------------
# Done
# --------------------------------------------------------------------------
EC2_IP=$(curl -s http://169.254.169.254/latest/meta-data/public-ipv4 2>/dev/null || echo "<your-ec2-ip>")

echo ""
echo "=========================================="
echo " Deployment complete!"
echo "=========================================="
echo ""
echo "  App URL   : http://$EC2_IP/"
echo "  Health    : http://$EC2_IP/api/health"
echo ""
echo "  Logs:"
echo "    sudo journalctl -u $SERVICE_NAME -f"
echo "    tail -f /var/log/campus-tracker/access.log"
echo "    tail -f /var/log/campus-tracker/error.log"
echo ""
echo "  IMPORTANT: Make sure your EC2 Security Group allows:"
echo "    - Inbound port 80  (HTTP)  from 0.0.0.0/0"
echo "    - Inbound port 443 (HTTPS) from 0.0.0.0/0  [optional, for SSL]"
echo ""
