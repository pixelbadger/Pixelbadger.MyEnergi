#!/usr/bin/env bash
# install.sh — set up the myenergi service as a systemd user unit
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SERVICE_NAME="myenergi"
UNIT_DIR="$HOME/.config/systemd/user"
UNIT_FILE="$UNIT_DIR/$SERVICE_NAME.service"
ENV_FILE="$SCRIPT_DIR/.env"

echo "==> Installing myenergi service from $SCRIPT_DIR"

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
ask()      { local v; read  -rp "  $1: " v;          echo "$v"; }
ask_secret(){ local v; read -rsp "  $1: " v; echo >&2; echo "$v"; }
ask_default(){ local v; read -rp "  $1 [$2]: " v;    echo "${v:-$2}"; }

if [ -f "$ENV_FILE" ]; then
    read -rp ".env already exists — reconfigure? [y/N] " RECONF
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
echo "  Dashboard:     http://localhost:$SERVICE_PORT"
echo ""
echo "Run 'systemctl --user start $SERVICE_NAME' to launch."
