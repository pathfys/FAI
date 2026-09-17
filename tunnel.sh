#!/bin/bash
# P2P Exchange — Auto Tunnel Script
# Starts cloudflared, captures the URL, updates backend .env + frontend meta tag,
# sets Telegram webhook, and optionally pushes frontend to GitHub.

set -e

PROJECT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$PROJECT_DIR"

ENV_FILE="$PROJECT_DIR/.env"
FRONTEND_FILE="$PROJECT_DIR/frontend.html"
TUNNEL_LOG="$PROJECT_DIR/tunnel.log"
TUNNEL_URL_FILE="$PROJECT_DIR/.tunnel_url"

# Read BOT_TOKEN from .env
BOT_TOKEN=$(grep '^BOT_TOKEN=' "$ENV_FILE" | cut -d'=' -f2- | tr -d '"' | tr -d "'")

if [ -z "$BOT_TOKEN" ]; then
    echo "ERROR: BOT_TOKEN not found in .env"
    exit 1
fi

echo "[tunnel] Stopping old cloudflared if running..."
pkill -f "cloudflared tunnel" 2>/dev/null || true
sleep 2

echo "[tunnel] Starting cloudflared tunnel on port 8000..."
cloudflared tunnel --url http://localhost:8000 --no-tls-verify > "$TUNNEL_LOG" 2>&1 &
TUNNEL_PID=$!

echo "[tunnel] Waiting for tunnel URL..."
TUNNEL_URL=""
for i in $(seq 1 30); do
    TUNNEL_URL=$(grep -oP 'https://[a-z0-9-]+\.trycloudflare\.com' "$TUNNEL_LOG" | head -1)
    if [ -n "$TUNNEL_URL" ]; then
        break
    fi
    sleep 1
done

if [ -z "$TUNNEL_URL" ]; then
    echo "ERROR: Could not get tunnel URL after 30 seconds"
    cat "$TUNNEL_LOG"
    exit 1
fi

echo "[tunnel] Got URL: $TUNNEL_URL"
echo "$TUNNEL_URL" > "$TUNNEL_URL_FILE"

# Update .env
echo "[tunnel] Updating .env..."
sed -i "s|^WEBAPP_URL=.*|WEBAPP_URL=$TUNNEL_URL|" "$ENV_FILE"
sed -i "s|^WEBHOOK_URL=.*|WEBHOOK_URL=$TUNNEL_URL|" "$ENV_FILE"

# Update frontend meta tag (API_BASE)
if [ -f "$FRONTEND_FILE" ]; then
    echo "[tunnel] Updating frontend API_BASE..."
    if grep -q 'name="api-base"' "$FRONTEND_FILE"; then
        sed -i "s|<meta name=\"api-base\" content=\"[^\"]*\"|<meta name=\"api-base\" content=\"$TUNNEL_URL\"|" "$FRONTEND_FILE"
    else
        sed -i "s|<meta charset=\"UTF-8\">|<meta charset=\"UTF-8\">\n<meta name=\"api-base\" content=\"$TUNNEL_URL\">|" "$FRONTEND_FILE"
    fi
fi

# Set Telegram webhook
echo "[tunnel] Setting Telegram webhook..."
WEBHOOK_RESP=$(curl -s "https://api.telegram.org/bot${BOT_TOKEN}/setWebhook?url=${TUNNEL_URL}/webhook")
echo "[tunnel] Webhook response: $WEBHOOK_RESP"

# Restart backend to pick up new .env
echo "[tunnel] Restarting backend..."
systemctl restart p2p-backend 2>/dev/null || true

# Push frontend to GitHub if repo exists
GITHUB_REPO="$PROJECT_DIR/fai-frontend"
if [ -d "$GITHUB_REPO/.git" ]; then
    echo "[tunnel] Pushing frontend to GitHub..."
    cp "$FRONTEND_FILE" "$GITHUB_REPO/index.html"
    cd "$GITHUB_REPO"
    git add index.html
    git commit -m "Update API_BASE to $TUNNEL_URL" 2>/dev/null || true
    git push origin main 2>/dev/null || git push origin master 2>/dev/null || true
    cd "$PROJECT_DIR"
    echo "[tunnel] Frontend pushed."
else
    echo "[tunnel] No GitHub repo at $GITHUB_REPO, skipping push."
    echo "[tunnel] To enable auto-push:"
    echo "  git clone https://github.com/pathfys/fai.git $GITHUB_REPO"
fi

echo ""
echo "========================================="
echo "  Tunnel active: $TUNNEL_URL"
echo "  Webhook set:   $TUNNEL_URL/webhook"
echo "  Backend:       http://localhost:8000"
echo "  Frontend:      $TUNNEL_URL"
echo "========================================="

# Keep running (cloudflared is in background)
wait $TUNNEL_PID
