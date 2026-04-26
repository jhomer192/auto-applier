#!/usr/bin/env bash
# Auto Job Applier — one-shot setup wizard
# Usage: ./setup.sh
# Or from scratch: bash <(curl -fsSL https://raw.githubusercontent.com/jhomer192/auto-applier/main/setup.sh)
set -euo pipefail

# ---------------------------------------------------------------------------
# Colours
# ---------------------------------------------------------------------------
RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'
BLUE='\033[0;34m'; BOLD='\033[1m'; DIM='\033[2m'; NC='\033[0m'

ok()   { echo -e "${GREEN}✔${NC}  $*"; }
info() { echo -e "${BLUE}→${NC}  $*"; }
warn() { echo -e "${YELLOW}⚠${NC}  $*"; }
fail() { echo -e "${RED}✖${NC}  $*"; exit 1; }
step() { echo -e "\n${BOLD}${BLUE}── $* ──${NC}"; }
hr()   { echo -e "${DIM}────────────────────────────────────────────────────${NC}"; }

# ---------------------------------------------------------------------------
# 0. Banner
# ---------------------------------------------------------------------------
clear
cat <<'BANNER'

  ╔══════════════════════════════════════════════════════╗
  ║         Auto Job Applier  —  Setup Wizard            ║
  ║  Finds jobs, writes tailored applications, taps Y/N  ║
  ╚══════════════════════════════════════════════════════╝

BANNER
echo "This script will:"
echo "  1. Install system and Python dependencies"
echo "  2. Build your candidate profile"
echo "  3. Connect your Telegram bot"
echo "  4. Optionally log in to LinkedIn and configure Gmail"
echo "  5. Install and start the background service"
echo ""
read -rp "Press Enter to begin, or Ctrl-C to abort."

# ---------------------------------------------------------------------------
# 1. Clone repo if we're running the script directly from the internet
#    (i.e. the script is not inside the repo directory yet)
# ---------------------------------------------------------------------------
REPO_URL="https://github.com/jhomer192/auto-applier.git"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

if [[ ! -f "$SCRIPT_DIR/requirements.txt" ]]; then
  step "Cloning repository"
  TARGET_DIR="$HOME/auto-applier"
  if [[ -d "$TARGET_DIR/.git" ]]; then
    info "Repo already exists at $TARGET_DIR — pulling latest"
    git -C "$TARGET_DIR" pull --ff-only
  else
    git clone "$REPO_URL" "$TARGET_DIR"
  fi
  cd "$TARGET_DIR"
  SCRIPT_DIR="$TARGET_DIR"
  ok "Repository ready at $TARGET_DIR"
else
  cd "$SCRIPT_DIR"
fi

# ---------------------------------------------------------------------------
# 2. Python version check
# ---------------------------------------------------------------------------
step "Checking Python"
if command -v python3.12 &>/dev/null; then
  PYTHON=python3.12
elif command -v python3.11 &>/dev/null; then
  PYTHON=python3.11
elif python3 --version 2>&1 | grep -qE '3\.(11|12|13)'; then
  PYTHON=python3
else
  info "Python 3.11+ not found — installing..."
  sudo apt-get update -qq
  sudo apt-get install -y python3.12 python3.12-venv || \
    sudo apt-get install -y python3.11 python3.11-venv python3.11-distutils
  command -v python3.12 &>/dev/null && PYTHON=python3.12 || PYTHON=python3.11
fi
ok "Using $PYTHON ($($PYTHON --version))"

# ---------------------------------------------------------------------------
# 3. System deps for Playwright
# ---------------------------------------------------------------------------
step "Installing system dependencies"
if dpkg -s libgbm1 &>/dev/null 2>&1; then
  ok "System dependencies already installed"
else
  info "Installing Playwright system libraries (requires sudo)..."
  sudo apt-get update -qq
  sudo apt-get install -y \
    libglib2.0-0 libnss3 libatk1.0-0 libatk-bridge2.0-0 \
    libcups2 libxkbcommon0 libxcomposite1 libxdamage1 \
    libxfixes3 libxrandr2 libgbm1 libasound2 libpango-1.0-0 \
    libcairo2 libgdk-pixbuf2.0-0 libgtk-3-0 2>/dev/null || \
  sudo apt-get install -y \
    libglib2.0-0 libnss3 libatk1.0-0 libatk-bridge2.0-0 \
    libcups2 libxkbcommon0 libxcomposite1 libxdamage1 \
    libxfixes3 libxrandr2 libgbm1 libpango-1.0-0 \
    libcairo2 libgdk-pixbuf-2.0-0 libgtk-3-0
  ok "System libraries installed"
fi

# ---------------------------------------------------------------------------
# 4. Python venv + dependencies
# ---------------------------------------------------------------------------
step "Setting up Python environment"
if [[ ! -d ".venv" ]]; then
  info "Creating virtual environment..."
  # Ensure venv module is available for the selected Python version
  if ! $PYTHON -m venv --help &>/dev/null 2>&1; then
    VER=$($PYTHON -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')
    sudo apt-get install -y "python${VER}-venv" 2>/dev/null || true
  fi
  $PYTHON -m venv .venv
fi
ok "Virtual environment ready"

info "Installing Python packages (this may take a minute)..."
.venv/bin/pip install --upgrade pip -q
.venv/bin/pip install -r requirements.txt -q
ok "Python packages installed"

info "Installing Playwright browser..."
.venv/bin/playwright install chromium 2>/dev/null || true
ok "Playwright ready"

mkdir -p data/screenshots

# ---------------------------------------------------------------------------
# 5. Quick smoke test
# ---------------------------------------------------------------------------
step "Running tests"
if .venv/bin/pytest --tb=short -q 2>&1 | tail -1 | grep -q "passed"; then
  ok "All tests passing"
else
  warn "Some tests failed — continuing anyway (non-fatal at setup time)"
fi

# ---------------------------------------------------------------------------
# 6. Profile setup
# ---------------------------------------------------------------------------
step "Building your candidate profile"
if [[ -f "profile.yaml" ]]; then
  warn "profile.yaml already exists."
  read -rp "  Re-run profile setup? [y/N] " redo_profile
  if [[ "${redo_profile,,}" != "y" ]]; then
    ok "Keeping existing profile.yaml"
  else
    .venv/bin/python setup/collect_profile.py
  fi
else
  .venv/bin/python setup/collect_profile.py
fi

# ---------------------------------------------------------------------------
# 7. Telegram bot credentials
# ---------------------------------------------------------------------------
step "Connecting your Telegram bot"
hr
echo "  You need a Telegram bot token and your personal chat ID."
echo ""
echo "  ${BOLD}Get a bot token:${NC}"
echo "    1. Open Telegram → search @BotFather → /newbot"
echo "    2. Give it a name (e.g. 'My Job Bot')"
echo "    3. Copy the token BotFather gives you"
echo ""

# Load existing .env if present
BOT_TOKEN=""
CHAT_ID=""
if [[ -f ".env" ]]; then
  BOT_TOKEN=$(grep "^TELEGRAM_BOT_TOKEN=" .env | cut -d= -f2- || true)
  CHAT_ID=$(grep "^TELEGRAM_CHAT_ID=" .env | cut -d= -f2- || true)
fi

if [[ -z "$BOT_TOKEN" ]]; then
  read -rp "  Paste your bot token: " BOT_TOKEN
  BOT_TOKEN="${BOT_TOKEN// /}"
fi

echo ""
echo "  ${BOLD}Get your chat ID:${NC}"
echo "    1. Send any message to your new bot in Telegram"
if [[ -n "$BOT_TOKEN" ]]; then
  echo "    2. Open: https://api.telegram.org/bot${BOT_TOKEN}/getUpdates"
fi
echo "    3. Find the number next to \"id\" inside result[0].message.from"
echo ""

if [[ -z "$CHAT_ID" ]]; then
  read -rp "  Paste your chat ID: " CHAT_ID
  CHAT_ID="${CHAT_ID// /}"
fi

# Write .env
cp .env.example .env
sed -i "s|^TELEGRAM_BOT_TOKEN=.*|TELEGRAM_BOT_TOKEN=${BOT_TOKEN}|" .env
sed -i "s|^TELEGRAM_CHAT_ID=.*|TELEGRAM_CHAT_ID=${CHAT_ID}|" .env
ok ".env written (token masked: ${BOT_TOKEN:0:10}...)"

# ---------------------------------------------------------------------------
# 8. Test Telegram connection
# ---------------------------------------------------------------------------
step "Testing Telegram connection"
if .venv/bin/python setup/test_telegram.py 2>&1; then
  ok "Telegram connection verified — you should have received a test message"
else
  warn "Telegram test failed. Double-check your token and chat ID in .env"
  echo "    Token:   $(grep TELEGRAM_BOT_TOKEN .env)"
  echo "    Chat ID: $(grep TELEGRAM_CHAT_ID .env)"
  read -rp "  Press Enter to continue anyway, or Ctrl-C to abort and re-run: "
fi

# ---------------------------------------------------------------------------
# 9. LinkedIn login (optional)
# ---------------------------------------------------------------------------
step "LinkedIn Easy Apply (optional)"
hr
echo "  LinkedIn Easy Apply fills and submits LinkedIn job applications."
echo "  Requires a one-time browser login. Your session is saved locally."
echo ""
read -rp "  Set up LinkedIn now? [y/N] " do_linkedin
if [[ "${do_linkedin,,}" == "y" ]]; then
  echo ""
  echo "  Do you have a display available? (desktop or Xvfb DISPLAY=:0)"
  read -rp "  [y/N] " has_display
  if [[ "${has_display,,}" == "y" ]]; then
    DISPLAY="${DISPLAY:-:0}" .venv/bin/python setup/linkedin_login.py && ok "LinkedIn auth saved" || warn "LinkedIn setup failed — you can re-run setup/linkedin_login.py later"
  else
    warn "Skipping LinkedIn login (no display). Run later:"
    echo "    DISPLAY=:0 .venv/bin/python setup/linkedin_login.py"
  fi
else
  info "Skipping LinkedIn. Re-run later: DISPLAY=:0 .venv/bin/python setup/linkedin_login.py"
fi

# ---------------------------------------------------------------------------
# 10. Systemd service
# ---------------------------------------------------------------------------
step "Installing background service"
REPO_PATH="$(pwd)"
PYTHON_PATH="$(pwd)/.venv/bin/python"

cp auto-applier.service /tmp/auto-applier.service
sed -i "s|REPO_PATH_PLACEHOLDER|${REPO_PATH}|g"    /tmp/auto-applier.service
sed -i "s|PYTHON_PATH_PLACEHOLDER|${PYTHON_PATH}|g" /tmp/auto-applier.service

if sudo cp /tmp/auto-applier.service /etc/systemd/system/ && \
   sudo systemctl daemon-reload && \
   sudo systemctl enable --now auto-applier; then
  sleep 2
  if systemctl is-active --quiet auto-applier; then
    ok "Service running (auto-applier.service)"
  else
    warn "Service installed but not active — check: journalctl -u auto-applier -n 30"
  fi
else
  warn "Could not install systemd service (maybe not root?)"
  echo "  Run manually: .venv/bin/python -m bot.main"
fi

# ---------------------------------------------------------------------------
# 11. Autonomous mode prompt
# ---------------------------------------------------------------------------
step "Configure autonomous mode"
hr
echo "  The bot can search for jobs and apply automatically — no Y/N needed."
echo "  Set your target roles and a score threshold (0–100)."
echo "  Jobs scoring ≥ threshold with all fields answerable → submitted silently."
echo "  Everything else → sent to you as a Telegram Y/N prompt."
echo ""
read -rp "  Enable autonomous mode now? [Y/n] " do_auto
if [[ "${do_auto,,}" != "n" ]]; then
  read -rp "  Desired roles (comma-separated, e.g. Software Engineer,Backend Engineer): " roles_raw
  read -rp "  Auto-apply score threshold 0-100 (e.g. 80; 0 = always ask): " threshold_raw
  threshold="${threshold_raw:-0}"

  if systemctl is-active --quiet auto-applier 2>/dev/null; then
    info "Sending preferences to bot via Telegram..."
    # Use the bot's own API to send the /prefs commands
    if [[ -n "$roles_raw" ]]; then
      curl -s "https://api.telegram.org/bot${BOT_TOKEN}/sendMessage" \
        -d "chat_id=${CHAT_ID}" \
        -d "text=/prefs roles ${roles_raw}" > /dev/null && ok "Roles set: $roles_raw"
    fi
    if [[ "$threshold" != "0" ]]; then
      curl -s "https://api.telegram.org/bot${BOT_TOKEN}/sendMessage" \
        -d "chat_id=${CHAT_ID}" \
        -d "text=/prefs autoapply ${threshold}" > /dev/null && ok "Auto-apply threshold: $threshold"
    fi
    curl -s "https://api.telegram.org/bot${BOT_TOKEN}/sendMessage" \
      -d "chat_id=${CHAT_ID}" \
      -d "text=/prefs autosearch on" > /dev/null && ok "Auto-search enabled"
  else
    warn "Service not running — set preferences manually after starting:"
    [[ -n "$roles_raw" ]] && echo "    /prefs roles $roles_raw"
    [[ "$threshold" != "0" ]] && echo "    /prefs autoapply $threshold"
    echo "    /prefs autosearch on"
  fi
else
  info "Skipping autonomous mode. Set manually with /prefs in Telegram."
fi

# ---------------------------------------------------------------------------
# 12. Done
# ---------------------------------------------------------------------------
echo ""
hr
echo -e "${GREEN}${BOLD}  Setup complete!${NC}"
hr
echo ""
echo "  ${BOLD}Your bot is running.${NC} Here's what it does:"
echo ""
echo "  → Every 30 min: searches LinkedIn for your target roles"
echo "  → Scores each posting: match quality, salary, fit, sponsorship"
echo "  → Auto-applies to jobs above your threshold (no Y/N)"
echo "  → Sends everything else as a Y/N prompt to Telegram"
echo ""
echo "  ${BOLD}Key commands in Telegram:${NC}"
echo "    /prefs roles <role1,role2>  — set target roles"
echo "    /prefs autoapply <0-100>    — auto-apply threshold"
echo "    /prefs show                 — view all preferences"
echo "    /queue                      — review pending discovered jobs"
echo "    /report                     — stats (today/week/all-time)"
echo "    /linkedin                   — audit your LinkedIn profile"
echo "    /website                    — generate a GitHub Pages portfolio"
echo "    /help                       — full command reference"
echo ""
echo "  ${BOLD}Logs:${NC}    journalctl -u auto-applier -f"
echo "  ${BOLD}Restart:${NC} sudo systemctl restart auto-applier"
echo "  ${BOLD}Status:${NC}  systemctl status auto-applier"
echo ""
echo "  ${DIM}Profile is at: $(pwd)/profile.yaml${NC}"
echo "  ${DIM}Database at:   $(pwd)/data/applications.db${NC}"
echo ""
hr
