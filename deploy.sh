#!/bin/bash
set -e

echo "=== P2P Exchange Deploy Script ==="
echo "Target: AlmaLinux 10 Server"
echo ""

# 1. System packages
echo "[1/6] Installing system dependencies..."
sudo dnf install -y python3.11 python3.11-pip git curl

# 2. Create project directory
echo "[2/6] Setting up project directory..."
PROJECT_DIR="$HOME/p2p-exchange"
mkdir -p "$PROJECT_DIR"
cd "$PROJECT_DIR"

# 3. Install Python dependencies
echo "[3/6] Installing Python packages..."
pip3.11 install --user aiogram fastapi uvicorn aiosqlite python-dotenv pydantic

# 4. Install cloudflared
echo "[4/6] Installing cloudflared..."
if ! command -v cloudflared &> /dev/null; then
    curl -L https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-x86_64 -o /usr/local/bin/cloudflared
    sudo chmod +x /usr/local/bin/cloudflared
    echo "cloudflared installed."
else
    echo "cloudflared already installed."
fi

# 5. Copy files
echo "[5/6] Copying project files..."
echo "Place these files in $PROJECT_DIR:"
echo "  - backend.py"
echo "  - frontend.html  (rename from frontend_binance.html)"
echo "  - .env  (copy from .env.example and fill in)"
echo "  - tunnel.sh"
echo ""

# 6. Create systemd services
echo "[6/6] Creating systemd services..."

# Backend service
sudo tee /etc/systemd/system/p2p-backend.service > /dev/null <<'UNIT'
[Unit]
Description=P2P Exchange Backend
After=network.target

[Service]
Type=simple
User=root
WorkingDirectory=/root/p2p-exchange
ExecStart=/usr/bin/python3.11 backend.py
Restart=always
RestartSec=5
Environment=PATH=/usr/local/bin:/usr/bin

[Install]
WantedBy=multi-user.target
UNIT

# Tunnel service
sudo tee /etc/systemd/system/p2p-tunnel.service > /dev/null <<'UNIT'
[Unit]
Description=P2P Cloudflare Tunnel
After=network.target p2p-backend.service
Requires=p2p-backend.service

[Service]
Type=simple
User=root
WorkingDirectory=/root/p2p-exchange
ExecStart=/bin/bash /root/p2p-exchange/tunnel.sh
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
UNIT

sudo systemctl daemon-reload
sudo systemctl enable p2p-backend p2p-tunnel

echo ""
echo "=== Deploy complete! ==="
echo ""
echo "Next steps:"
echo "  1. Copy files to $PROJECT_DIR"
echo "  2. Rename frontend_binance.html -> frontend.html"
echo "  3. Copy .env.example -> .env and fill in BOT_TOKEN, ADMIN_IDS, LOG_CHANNEL_ID"
echo "  4. Run: sudo systemctl start p2p-backend"
echo "  5. Run: sudo systemctl start p2p-tunnel"
echo "  6. Check logs: journalctl -u p2p-backend -f"
echo "  7. Check tunnel: journalctl -u p2p-tunnel -f"
