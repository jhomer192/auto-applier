#!/usr/bin/env bash
# 3am VPS janitor — deterministic cleanup, no model, no judgment calls.
# Run by applier-janitor.timer as root. Posts to Discord ONLY when it fixed
# something or a problem remains; a quiet night posts nothing.
set -uo pipefail

ROOT=/opt/auto-applier
LOG="$ROOT/logs/janitor.log"
mkdir -p "$ROOT/logs"

note() { echo "[$(date -Is)] $1" >> "$LOG"; ACTIONS+=("$1"); }
quiet_log() { echo "[$(date -Is)] $1" >> "$LOG"; }
ACTIONS=()

# ── 1. warp-svc (applier's SOCKS proxy — has been OOM-killed before) ─────────
if ! systemctl is-active --quiet warp-svc; then
  systemctl restart warp-svc
  sleep 5
  if ss -tln | grep -q 127.0.0.1:40000; then
    note "🔧 warp-svc was dead — restarted, port 40000 listening again"
  else
    note "⚠️ warp-svc was dead — restart did NOT bring port 40000 back"
  fi
fi

# ── 2. Live bot services: restart only if DEAD, never touch running ones ─────
for svc in claude-discord@assistant auto-applier-brain; do
  if ! systemctl is-active --quiet "$svc"; then
    systemctl restart "$svc"
    sleep 3
    if systemctl is-active --quiet "$svc"; then
      note "🔧 $svc was dead — restarted OK"
    else
      note "⚠️ $svc was dead — restart FAILED (journalctl -u $svc)"
    fi
  fi
done

# ── 3. Disk: prune known-safe artifacts when above 80% ───────────────────────
disk_pct() { df --output=pcent / | tail -1 | tr -dc 0-9; }
BEFORE=$(disk_pct)
if [ "$BEFORE" -gt 80 ]; then
  rm -f "$ROOT"/*.png "$ROOT"/*_snapshot.md
  find "$ROOT/.playwright-mcp" -mindepth 1 -mtime +2 -delete 2>/dev/null
  journalctl --vacuum-size=200M >/dev/null 2>&1
  apt-get clean >/dev/null 2>&1
  AFTER=$(disk_pct)
  note "🔧 disk was ${BEFORE}% — pruned run artifacts/journal/apt cache → ${AFTER}%"
  [ "$AFTER" -gt 80 ] && note "⚠️ disk still ${AFTER}% after safe pruning — needs a human look (or a bigger disk)"
fi

# ── 4. Orphaned headless browsers (no claude run alive to own them) ──────────
if ! pgrep -f "claude [-]p" >/dev/null && pgrep -f "chrome.*--headless" >/dev/null; then
  N=$(pgrep -cf "chrome.*--headless")
  pkill -f "playwright-mcp" 2>/dev/null
  pkill -f "chrome.*--headless" 2>/dev/null
  note "🔧 killed $N orphaned headless chrome process(es) (no claude run in flight)"
fi

# ── 5. Root-owned droppings back to the claude user ──────────────────────────
if find "$ROOT/data" "$ROOT/logs" -user root -print -quit 2>/dev/null | grep -q .; then
  chown -R claude:claude "$ROOT/data" "$ROOT/logs"
  note "🔧 chowned root-owned files in data/ + logs/ back to claude"
fi

# ── 6. Timers still scheduled ─────────────────────────────────────────────────
for t in applier-nightly.timer applier-janitor.timer; do
  systemctl list-timers "$t" --no-pager 2>/dev/null | grep -q "$t" \
    || note "⚠️ $t has no scheduled next run — re-enable it"
done

MEM=$(free -h | awk '/^Mem:/{print $7" free"}')
quiet_log "JANITOR_DONE disk=$(disk_pct)% mem=$MEM actions=${#ACTIONS[@]}"

# ── Report to Discord only when something happened ───────────────────────────
if [ "${#ACTIONS[@]}" -gt 0 ]; then
  TOKEN=$(grep '^DISCORD_BOT_TOKEN=' "$ROOT/.env" | cut -d= -f2-)
  CHANNEL=$(grep '^DISCORD_CHANNEL_ID=' "$ROOT/.env" | cut -d= -f2-)
  if [ -n "$TOKEN" ] && [ -n "$CHANNEL" ]; then
    BODY="🧹 **3am janitor** (disk $(disk_pct)%, mem $MEM)"
    for a in "${ACTIONS[@]}"; do BODY="$BODY"$'\n'"$a"; done
    printf '%s' "$BODY" | head -c 1900 | CH="$CHANNEL" TK="$TOKEN" python3 -c '
import json,sys,urllib.request,os
content = sys.stdin.read()
req = urllib.request.Request(
    "https://discord.com/api/v10/channels/%s/messages" % os.environ["CH"],
    data=json.dumps({"content": content}).encode(),
    headers={"Authorization": "Bot " + os.environ["TK"], "Content-Type": "application/json"})
urllib.request.urlopen(req)
' 2>>"$LOG" || quiet_log "DISCORD_POST_ERR"
  fi
fi
exit 0
