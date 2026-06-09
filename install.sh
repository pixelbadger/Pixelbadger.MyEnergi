#!/usr/bin/env bash
# install.sh — set up the myenergi service as a systemd user unit
#
# Run from a checkout:   bash install.sh
# Or straight from GitHub:
#   curl -fsSL https://raw.githubusercontent.com/pixelbadger/Pixelbadger.MyEnergi/master/install.sh | bash
#
# Overrides: MYENERGI_INSTALL_DIR (default ~/myenergi), MYENERGI_REPO_URL
set -euo pipefail

REPO_URL="${MYENERGI_REPO_URL:-https://github.com/pixelbadger/Pixelbadger.MyEnergi.git}"
INSTALL_DIR="${MYENERGI_INSTALL_DIR:-$HOME/myenergi}"
SERVICE_NAME="myenergi"
UNIT_DIR="$HOME/.config/systemd/user"
UNIT_FILE="$UNIT_DIR/$SERVICE_NAME.service"

# --- 0. Locate or fetch the source -------------------------------------------
# When piped (curl | bash) BASH_SOURCE is unset and there's no checkout to run
# from, so clone (or update) the repo first.
if [ -n "${BASH_SOURCE[0]:-}" ] && [ -f "$(dirname "${BASH_SOURCE[0]}")/service.py" ]; then
    SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
else
    if ! command -v git >/dev/null 2>&1; then
        echo "==> Installing git..."
        sudo apt install -y git
    fi
    if [ -d "$INSTALL_DIR/.git" ]; then
        echo "==> Updating existing checkout in $INSTALL_DIR"
        git -C "$INSTALL_DIR" pull --ff-only
    elif [ -e "$INSTALL_DIR" ]; then
        echo "ERROR: $INSTALL_DIR exists but is not a git checkout." >&2
        echo "Set MYENERGI_INSTALL_DIR to choose a different location." >&2
        exit 1
    else
        echo "==> Cloning $REPO_URL into $INSTALL_DIR"
        git clone "$REPO_URL" "$INSTALL_DIR"
    fi
    SCRIPT_DIR="$INSTALL_DIR"
fi

ENV_FILE="$SCRIPT_DIR/.env"

echo "==> Installing myenergi service from $SCRIPT_DIR"

# When piped, stdin is the script itself — interactive prompts must come from
# the terminal instead.
if { exec 3</dev/tty; } 2>/dev/null; then
    exec 3<&-
    HAVE_TTY=1
else
    HAVE_TTY=0
fi

# --- 1. System Python packages ----------------------------------------------
# We use the system Python3 directly (avoids venv pyc corruption on Android fs).
echo "==> Checking Python dependencies..."
MISSING=""
for pkg in flask apscheduler dotenv requests; do
    python3 -B -c "import $pkg" 2>/dev/null || MISSING="$MISSING $pkg"
done
if [ -n "$MISSING" ]; then
    echo "==> Installing missing packages:$MISSING"
    sudo apt install -y python3-flask python3-apscheduler python3-dotenv python3-requests
fi

# --- 2. .env configuration --------------------------------------------------
ask()       { local v; read  -rp "  $1: " v </dev/tty;          echo "$v"; }
ask_secret(){ local v; read -rsp "  $1: " v </dev/tty; echo >&2; echo "$v"; }
ask_default(){ local v; read -rp "  $1 [$2]: " v </dev/tty;     echo "${v:-$2}"; }

if [ "$HAVE_TTY" = 0 ]; then
    if [ -f "$ENV_FILE" ]; then
        echo "==> No terminal available — keeping existing .env"
        RECONF="n"
    else
        echo "ERROR: no terminal available to prompt for .env configuration." >&2
        echo "Re-run interactively, or create $ENV_FILE first (see README)." >&2
        exit 1
    fi
elif [ -f "$ENV_FILE" ]; then
    read -rp ".env already exists — reconfigure? [y/N] " RECONF </dev/tty
    [[ "${RECONF,,}" == "y" ]] || RECONF="n"
else
    RECONF="y"
fi

if [ "$RECONF" = "y" ]; then
    echo ""
    echo "==> MyEnergi hub  (myaccount.myenergi.com → Products → hub → API key)"
    HUB_SERIAL=$(ask        "Hub serial (starts 10...)")
    API_KEY=$(ask_secret    "API key")

    echo ""
    echo "==> Libbi"
    LIBBI_SERIAL=$(ask         "Libbi serial")
    LIBBI_CAP=$(ask_default    "Battery capacity kWh" "10.0")

    echo ""
    echo "==> myenergi account login  (for charge-from-grid control)"
    APP_EMAIL=$(ask            "Account email")
    APP_PASSWORD=$(ask_secret  "Account password")

    echo ""
    echo "==> Solcast  (solcast.com/rooftop-solar/dashboard)"
    SOLCAST_RESOURCE_ID=$(ask        "Site resource ID (UUID)")
    SOLCAST_API_KEY=$(ask_secret     "API key")

    echo ""
    echo "==> Weather  (optional — enables cold-weather battery pre-warm)"
    WEATHER_LAT=$(ask_default  "Property latitude (blank to skip)" "")
    WEATHER_LON=$(ask_default  "Property longitude (blank to skip)" "")

    echo ""
    echo "==> Service settings"
    SERVICE_PORT=$(ask_default   "Dashboard port" "5000")
    SERVICE_TOKEN=$(ask_secret   "API auth token (for /api/trigger-decision)")
    SYNC_INTERVAL=$(ask_default  "History sync interval hours" "4")

    cat > "$ENV_FILE" <<ENVEOF
MYENERGI_HUB_SERIAL=$HUB_SERIAL
MYENERGI_API_KEY=$API_KEY
MYENERGI_LIBBI_SERIAL=$LIBBI_SERIAL
MYENERGI_APP_EMAIL=$APP_EMAIL
MYENERGI_APP_PASSWORD=$APP_PASSWORD
LIBBI_CAPACITY_KWH=$LIBBI_CAP
SOLCAST_RESOURCE_ID=$SOLCAST_RESOURCE_ID
SOLCAST_API_KEY=$SOLCAST_API_KEY
SERVICE_PORT=$SERVICE_PORT
SERVICE_TOKEN=$SERVICE_TOKEN
SYNC_INTERVAL_HOURS=$SYNC_INTERVAL
DRY_RUN=true
WEATHER_LAT=$WEATHER_LAT
WEATHER_LON=$WEATHER_LON
PREWARM_THRESHOLD_C=2.0
PREWARM_LEAD_MINUTES=120

# Tariff — 3-band model, defaults = Octopus Flux. Edit to match your tariff.
# Hours are end-exclusive integers; off-peak must start 01:00+ and not wrap midnight.
TARIFF_OFFPEAK_START=2
TARIFF_OFFPEAK_END=5
TARIFF_PEAK_START=16
TARIFF_PEAK_END=19
TARIFF_OFFPEAK_P=18.0
TARIFF_STANDARD_P=29.0
TARIFF_PEAK_P=36.0
ENVEOF

    chmod 600 "$ENV_FILE"
    echo "==> .env written (DRY_RUN=true — flip to false once you've verified the API works)"
fi

# --- 3. systemd user unit ---------------------------------------------------
mkdir -p "$UNIT_DIR"

cat > "$UNIT_FILE" <<EOF
[Unit]
Description=MyEnergi Home Energy Service
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
WorkingDirectory=$SCRIPT_DIR
ExecStart=/usr/bin/python3 -B $SCRIPT_DIR/service.py
Restart=on-failure
RestartSec=30s
StandardOutput=journal
StandardError=journal
Environment=PYTHONDONTWRITEBYTECODE=1

[Install]
WantedBy=default.target
EOF

echo "==> systemd unit written to $UNIT_FILE"

# --- 4. Enable lingering so the unit runs without a login session -----------
loginctl enable-linger "$USER" 2>/dev/null || true

# --- 5. Reload and enable ---------------------------------------------------
systemctl --user daemon-reload
systemctl --user enable "$SERVICE_NAME"

echo ""
echo "Installation complete. Useful commands:"
echo ""
echo "  Start now:     systemctl --user start $SERVICE_NAME"
echo "  Stop:          systemctl --user stop $SERVICE_NAME"
echo "  Status:        systemctl --user status $SERVICE_NAME"
echo "  Live logs:     journalctl --user -u $SERVICE_NAME -f"
echo "  Dashboard:     http://localhost:${SERVICE_PORT:-5000}"
echo ""
echo "Run 'systemctl --user start $SERVICE_NAME' to launch."
