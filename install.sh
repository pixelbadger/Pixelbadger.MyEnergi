#!/usr/bin/env bash
# install.sh — set up the myenergi service as a systemd user unit
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SERVICE_NAME="myenergi"
UNIT_DIR="$HOME/.config/systemd/user"
UNIT_FILE="$UNIT_DIR/$SERVICE_NAME.service"
VENV="$SCRIPT_DIR/.venv"

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

# --- 2. .env check ----------------------------------------------------------
if [ ! -f "$SCRIPT_DIR/.env" ]; then
    echo "WARNING: .env not found — copy .env.example and fill in your credentials."
    echo "         The service will fail to start until .env exists."
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
echo "  Dashboard:     http://localhost:5000"
echo ""
echo "Run 'systemctl --user start $SERVICE_NAME' to launch."
