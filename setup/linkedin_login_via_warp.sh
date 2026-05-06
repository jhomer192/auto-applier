#!/bin/bash
# Bootstrap LinkedIn auth on a flagged-IP VPS by routing the headless login
# through Cloudflare WARP via wireproxy (process-isolated, no system route
# changes — your SSH session is untouched).
#
# Usage (from anywhere on the VPS, repo root auto-detected):
#   LINKEDIN_EMAIL='you@example.com' \
#   LINKEDIN_PASSWORD='your-password' \
#   bash setup/linkedin_login_via_warp.sh
#
# What it does:
#   1. Downloads wireproxy + wgcf into a scratch dir (~14MB total)
#   2. Registers a free Cloudflare WARP account (no signup, no email)
#   3. Starts wireproxy locally as a SOCKS5 endpoint on 127.0.0.1:25344
#   4. Runs setup/linkedin_login.py with PLAYWRIGHT_PROXY set to that endpoint
#   5. Handles 2FA via Telegram (you reply "code 123456" to the bot)
#   6. Tears down wireproxy and removes the scratch dir on exit
#
# Why this exists: LinkedIn flags datacenter IPs (Hetzner, DigitalOcean, etc.)
# at the login page and shows an infinite "Let's do a quick security check"
# spinner that never resolves for headless browsers. WARP gives Cloudflare
# routing for the one-time login; once cookies are saved they're IP-agnostic
# and the bot uses your VPS's normal IP for all subsequent traffic.

set -euo pipefail

if [[ -z "${LINKEDIN_EMAIL:-}" || -z "${LINKEDIN_PASSWORD:-}" ]]; then
  echo "ERROR: LINKEDIN_EMAIL and LINKEDIN_PASSWORD env vars are required."
  echo
  echo "Run with:"
  echo "  LINKEDIN_EMAIL='you@example.com' LINKEDIN_PASSWORD='...' bash setup/linkedin_login_via_warp.sh"
  exit 1
fi

# Resolve repo root: this script lives at setup/linkedin_login_via_warp.sh
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
if [[ ! -f "$REPO_ROOT/requirements.txt" || ! -d "$REPO_ROOT/bot" ]]; then
  echo "ERROR: Couldn't find auto-applier repo root from $SCRIPT_DIR."
  echo "Run this script from inside the cloned repo."
  exit 1
fi

if [[ ! -x "$REPO_ROOT/.venv/bin/python" ]]; then
  echo "ERROR: $REPO_ROOT/.venv not found. Run setup.sh first to build the venv."
  exit 1
fi

WORK="$(mktemp -d)"
WP_PID=""
cleanup() {
  if [[ -n "$WP_PID" ]] && kill -0 "$WP_PID" 2>/dev/null; then
    kill "$WP_PID" 2>/dev/null || true
  fi
  rm -rf "$WORK"
}
trap cleanup EXIT

cd "$WORK"

echo "==> Fetching wireproxy and wgcf..."
curl -sL -o wireproxy.tgz \
  https://github.com/windtf/wireproxy/releases/download/v1.1.2/wireproxy_linux_amd64.tar.gz
tar xzf wireproxy.tgz
WGCF_URL=$(curl -sL https://api.github.com/repos/ViRb3/wgcf/releases/latest \
  | grep '"browser_download_url".*linux_amd64' \
  | head -1 \
  | sed 's/.*"\(http[^"]*\)".*/\1/')
curl -sL -o wgcf "$WGCF_URL"
chmod +x wgcf wireproxy

echo "==> Registering free Cloudflare WARP account..."
./wgcf register --accept-tos > /dev/null
./wgcf generate > /dev/null

cat wgcf-profile.conf > wireproxy.conf
cat >> wireproxy.conf <<EOF

[Socks5]
BindAddress = 127.0.0.1:25344
EOF

echo "==> Starting wireproxy (SOCKS5 on 127.0.0.1:25344)..."
./wireproxy -c wireproxy.conf > wireproxy.log 2>&1 &
WP_PID=$!
sleep 4

WARP_IP=$(curl -s --max-time 8 --socks5 127.0.0.1:25344 https://www.cloudflare.com/cdn-cgi/trace 2>/dev/null | grep '^ip=' | cut -d= -f2 || true)
if [[ -z "$WARP_IP" ]]; then
  echo "ERROR: wireproxy didn't come up cleanly. Tail of log:"
  tail -10 wireproxy.log
  exit 1
fi
echo "    WARP IP: $WARP_IP (browser traffic only — system routing unchanged)"

echo "==> Running LinkedIn login through proxy..."
cd "$REPO_ROOT"
set -a
[[ -f .env ]] && source .env
set +a
PLAYWRIGHT_PROXY='socks5://127.0.0.1:25344' \
PYTHONPATH="$REPO_ROOT" \
.venv/bin/python setup/linkedin_login.py

echo
echo "==> Done. Auth state saved to $REPO_ROOT/data/linkedin_auth.json"
echo "    Cookies are IP-agnostic — the bot will run on your normal VPS IP from here."
