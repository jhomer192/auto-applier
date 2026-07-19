#!/usr/bin/env bash
# Resilient browser launcher for the auto-applier.
#
# Uses the home residential SOCKS tunnel (exposed on this VPS at 127.0.0.1:1080)
# when it is up, so applies egress from a genuine residential IP and get past
# Ashby/Lever datacenter-IP blocks. When the tunnel is DOWN it falls back to a
# DIRECT connection (no proxy) so an apply NEVER hard-fails just because the
# tunnel dropped. The choice is made fresh every time the browser is launched.
#
# The health check MUST send real traffic through the proxy. sshd keeps the
# 1080 listener bound even after the home box goes away (sleep, WSL shutdown,
# dead TCP), so a "is the port open" test passes while the tunnel routes
# nothing -- Chrome then hangs forever on every request, DNS included, and we
# never reach the direct-connection fallback below.
set -u

PROXY_PORT=1080
STATUS_FILE=/opt/auto-applier/data/proxy_status
PROBE_URL=https://api.ipify.org
PROBE_TIMEOUT=8

# End-to-end probe: does the proxy actually carry traffic to the internet?
# Two attempts so a single transient blip doesn't drop us to the VPS IP.
probe_proxy() {
  local attempt ip
  for attempt in 1 2; do
    ip=$(curl -s --max-time "$PROBE_TIMEOUT" \
           --socks5-hostname "127.0.0.1:${PROXY_PORT}" "$PROBE_URL" 2>/dev/null)
    if [[ "$ip" =~ ^[0-9]{1,3}(\.[0-9]{1,3}){3}$ ]]; then
      echo "$ip"
      return 0
    fi
  done
  return 1
}

PROXY_ARGS=()
if nc -z -w2 127.0.0.1 "$PROXY_PORT" 2>/dev/null && EGRESS_IP=$(probe_proxy); then
  PROXY_ARGS=(--proxy-server "socks5://127.0.0.1:${PROXY_PORT}")
  echo "up ${EGRESS_IP}" > "$STATUS_FILE" 2>/dev/null || true
  echo "[browser_launch] $(date -u +%FT%TZ) proxy UP, egress ${EGRESS_IP}" >&2
else
  echo "down" > "$STATUS_FILE" 2>/dev/null || true
  echo "[browser_launch] $(date -u +%FT%TZ) proxy DOWN, launching DIRECT (VPS IP)" >&2
fi

exec xvfb-run -a --server-args="-screen 0 1366x768x24" \
  npx -y @playwright/mcp@latest \
  --browser chromium \
  --no-sandbox \
  --user-agent "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36" \
  --viewport-size 1366x768 \
  --init-script /opt/auto-applier/stealth_init.js \
  --user-data-dir /opt/auto-applier/data/browser_profile \
  "${PROXY_ARGS[@]}"
