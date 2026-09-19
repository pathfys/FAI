#!/bin/bash
set -euo pipefail

echo "=== P2P Exchange Deploy (AlmaLinux 10) ==="

PROJECT_DIR="${PROJECT_DIR:-/root/p2p-exchange}"

echo "[1/5] Installing system dependencies..."
dnf install -y python3 python3-pip git curl

PY=$(command -v python3)
echo "      Using: $PY ($($PY --version))"

echo "[2/5] Installing Python packages..."
"$PY" -m pip install --upgrade pip
"$PY" -m pip install aiogram fastapi uvicorn aiosqlite python-dotenv pydantic

echo "[3/5] Installing cloudflared..."
if ! command -v cloudflared &> /dev/null; then
    ARCH=$(uname -m)
    case "$ARCH" in
        x86_64)  CF_ARCH=amd64 ;;
        aarch64) CF_ARCH=arm64 ;;
        *) echo "Unsupported arch: $ARCH"; exit 1 ;;
    esac
    curl -fsSL "https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-${CF_ARCH}" \
        -o /usr/local/bin/cloudflared
    chmod +x /usr/local/bin/cloudflared
fi
echo "      cloudflared: $(cloudflared --version 2>&1 | head -1)"

echo "[4/5] Checking project files..."
cd "$PROJECT_DIR"
for f in backend.py frontend.html tunnel.sh; do
    [ -f "$f" ] || { echo "ERROR: $f missing in $PROJECT_DIR"; exit 1; }
done
if [ ! -f .env ]; then
    cp .env.example .env
    echo "      Created .env from template — FILL IN BOT_TOKEN AND ADMIN_IDS!"
fi
chmod +x tunnel.sh

echo "[5/5] Creating systemd services..."

cat > /etc/systemd/system/p2p-backend.service <<UNIT
[Unit]
Description=P2P Exchange Backend
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
WorkingDirectory=$PROJECT_DIR
EnvironmentFile=$PROJECT_DIR/.env
ExecStart=$PY $PROJECT_DIR/backend.py
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
UNIT

cat > /etc/systemd/system/p2p-tunnel.service <<UNIT
[Unit]
Description=P2P Cloudflare Tunnel
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
WorkingDirectory=$PROJECT_DIR
ExecStart=/bin/bash $PROJECT_DIR/tunnel.sh
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
UNIT

systemctl daemon-reload
systemctl enable p2p-backend p2p-tunnel

echo ""
echo "=== Deploy complete ==="
echo ""
echo "Next:"
echo "  1. nano $PROJECT_DIR/.env    # BOT_TOKEN, ADMIN_IDS, LOG_CHANNEL_ID"
echo "  2. systemctl start p2p-backend"
echo "  3. systemctl start p2p-tunnel"
echo "  4. journalctl -u p2p-tunnel -f    # shows the public URL"
