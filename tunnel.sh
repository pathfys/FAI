#!/bin/bash
# Starts a cloudflared quick tunnel, then propagates the generated URL to:
#   .env (WEBAPP_URL + WEBHOOK_URL) -> frontend.html (api-base meta) -> Telegram webhook
# Finally restarts the backend so it picks up the new URL.
set -uo pipefail

PROJECT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$PROJECT_DIR"

ENV_FILE="$PROJECT_DIR/.env"
FRONTEND_FILE="$PROJECT_DIR/frontend.html"
TUNNEL_LOG="$PROJECT_DIR/tunnel.log"

[ -f "$ENV_FILE" ] || { echo "ERROR: .env not found"; exit 1; }

BOT_TOKEN=$(grep -E '^BOT_TOKEN=' "$ENV_FILE" | cut -d'=' -f2- | tr -d '"'"'" | xargs)
if [ -z "${BOT_TOKEN:-}" ] || [ "$BOT_TOKEN" = "YOUR_BOT_TOKEN_HERE" ]; then
    echo "ERROR: BOT_TOKEN is not set in .env"
    exit 1
fi

echo "[tunnel] Stopping any previous cloudflared..."
pkill -f "cloudflared tunnel" 2>/dev/null || true
sleep 2

echo "[tunnel] Starting quick tunnel on http://localhost:8000 ..."
: > "$TUNNEL_LOG"
cloudflared tunnel --url http://localhost:8000 --no-autoupdate >> "$TUNNEL_LOG" 2>&1 &
TUNNEL_PID=$!

echo "[tunnel] Waiting for public URL..."
TUNNEL_URL=""
for _ in $(seq 1 60); do
    TUNNEL_URL=$(grep -oE 'https://[a-z0-9-]+\.trycloudflare\.com' "$TUNNEL_LOG" | head -1)
    [ -n "$TUNNEL_URL" ] && break
    if ! kill -0 "$TUNNEL_PID" 2>/dev/null; then
        echo "ERROR: cloudflared exited early"
        cat "$TUNNEL_LOG"
        exit 1
    fi
    sleep 1
done

if [ -z "$TUNNEL_URL" ]; then
    echo "ERROR: no tunnel URL after 60s"
    cat "$TUNNEL_LOG"
    kill "$TUNNEL_PID" 2>/dev/null || true
    exit 1
fi

echo "[tunnel] URL: $TUNNEL_URL"

echo "[tunnel] Updating .env ..."
sed -i "s|^WEBAPP_URL=.*|WEBAPP_URL=$TUNNEL_URL|" "$ENV_FILE"
sed -i "s|^WEBHOOK_URL=.*|WEBHOOK_URL=$TUNNEL_URL|" "$ENV_FILE"

if [ -f "$FRONTEND_FILE" ]; then
    echo "[tunnel] Updating frontend api-base ..."
    if grep -q 'name="api-base"' "$FRONTEND_FILE"; then
        sed -i "s|<meta name=\"api-base\" content=\"[^\"]*\">|<meta name=\"api-base\" content=\"$TUNNEL_URL\">|" "$FRONTEND_FILE"
    else
        sed -i "0,/<meta charset=\"UTF-8\">/s||<meta charset=\"UTF-8\">\n<meta name=\"api-base\" content=\"$TUNNEL_URL\">|" "$FRONTEND_FILE"
    fi
fi

echo "[tunnel] Restarting backend ..."
systemctl restart p2p-backend 2>/dev/null || echo "[tunnel] (backend service not managed by systemd, skipping)"
sleep 3

echo "[tunnel] Setting Telegram webhook ..."
curl -fsS -X POST "https://api.telegram.org/bot${BOT_TOKEN}/setWebhook" \
     -d "url=${TUNNEL_URL}/webhook" || echo "[tunnel] webhook call failed"
echo ""

# Optional: push the updated frontend to GitHub (needs AUTO_PUSH=1 and working git creds)
if [ "${AUTO_PUSH:-0}" = "1" ] && [ -d "$PROJECT_DIR/.git" ]; then
    echo "[tunnel] Pushing frontend to GitHub ..."
    BRANCH=$(git -C "$PROJECT_DIR" rev-parse --abbrev-ref HEAD)
    git -C "$PROJECT_DIR" add frontend.html \
      && git -C "$PROJECT_DIR" commit -m "chore: point api-base at $TUNNEL_URL" \
      && git -C "$PROJECT_DIR" push origin "$BRANCH" \
      || echo "[tunnel] push skipped (nothing to commit or no credentials)"
fi

cat <<EOF

=========================================
  Public URL : $TUNNEL_URL
  Webhook    : $TUNNEL_URL/webhook
  Mini App   : $TUNNEL_URL
  Health     : $TUNNEL_URL/health
=========================================

EOF

wait "$TUNNEL_PID"
