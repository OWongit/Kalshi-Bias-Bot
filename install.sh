#!/bin/bash
set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$SCRIPT_DIR"

# ---------------------------------------------------------------------------
# Optional: DISPLAY_NAME for the installer banner only.
#
# This script:
#   - Creates a Python virtual environment (.venv)
#   - Installs all libraries from requirements.txt
#   - Prepares launcher.sh (optional helper)
#   - Installs and enables a systemd service so main.py starts at boot (headless,
#     no desktop / terminal autostart)
# ---------------------------------------------------------------------------
DISPLAY_NAME="${DISPLAY_NAME:-Kalshi Trading Bot}"

# ANSI colors: neon green for logo, gold for highlights
NEON_GREEN='\033[92m'
GOLD='\033[33m'
RESET='\033[0m'

# Logo (neon green)
echo -e "${NEON_GREEN}"
echo "#####       ######                   #####                 #####             #### 
#####     #######                    #####                 #####             #### 
#####    ######                      #####                 #####                 
#####  ######         ###########    #####   ###########   ##############   ##### 
############        ##############   ##### ##############  ###############  ##### 
#############       #####     #####  ##### ######    ####  #####     ###### ##### 
##############        #############  #####  ############   #####      ##### ##### 
######   ######     ###############  #####     ########### #####      ##### ##### 
#####     ######    #####    ######  ##### #####     ##### #####      ##### ##### 
#####      #######  ################ #####  #############  #####      ##### ##### 
#####        ######  ######### ##### #####   ###########   #####      ##### #####"
echo -e "${RESET}"

BANNER="  ${DISPLAY_NAME} Software Installer (Raspberry Pi 5)"
SEP=$(printf '%*s' ${#BANNER} '' | tr ' ' '=')
echo -e "${GOLD}${SEP}${RESET}"
echo -e "${GOLD}${BANNER}${RESET}"
echo -e "${GOLD}${SEP}${RESET}"

# System packages required for Python venv support
echo ""
echo -e "${GOLD}[1/4] Installing system dependencies...${RESET}"
sudo apt-get update
sudo apt-get install -y \
    python3 \
    python3-venv \
    python3-pip \
    python3-dev \
    libgpiod-dev \
    git

# Create virtual environment
VENV_DIR="$SCRIPT_DIR/.venv"
if [ -d "$VENV_DIR" ]; then
    echo ""
    echo -e "${GOLD}[2/4] Virtual environment already exists at $VENV_DIR, recreating...${RESET}"
    rm -rf "$VENV_DIR"
fi
echo ""
echo -e "${GOLD}[2/4] Creating virtual environment...${RESET}"
python3 -m venv "$VENV_DIR"

# Activate and install Python packages
echo ""
echo -e "${GOLD}[3/4] Installing Python dependencies...${RESET}"
source "$VENV_DIR/bin/activate"
pip install --upgrade pip
pip install -r "$SCRIPT_DIR/requirements.txt"

# Fix line endings and make launcher executable
echo "  Preparing launcher script..."
sed -i 's/\r$//' "$SCRIPT_DIR/launcher.sh"
chmod +x "$SCRIPT_DIR/launcher.sh"

# Remove legacy desktop autostart if a previous installer version created it
AUTOSTART_FILE="$HOME/.config/autostart/kalshi-trader.desktop"
if [ -f "$AUTOSTART_FILE" ]; then
    echo "  Removing legacy autostart: $AUTOSTART_FILE"
    rm -f "$AUTOSTART_FILE"
fi

# Headless startup at boot (systemd — no GUI / terminal session required)
SERVICE_NAME="kalshi-trading-bot"
SERVICE_PATH="/etc/systemd/system/${SERVICE_NAME}.service"
RUN_AS_USER="${SUDO_USER:-$USER}"

echo ""
echo -e "${GOLD}[4/4] Installing systemd service (headless boot)...${RESET}"
sudo tee "$SERVICE_PATH" > /dev/null <<EOF
[Unit]
Description=${DISPLAY_NAME} (headless)
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=${RUN_AS_USER}
WorkingDirectory=${SCRIPT_DIR}
ExecStart=${SCRIPT_DIR}/.venv/bin/python ${SCRIPT_DIR}/main.py
Restart=always
RestartSec=10

# Logs: journalctl -u ${SERVICE_NAME} -f
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
EOF

sudo systemctl daemon-reload
sudo systemctl enable "${SERVICE_NAME}.service"
if ! sudo systemctl start "${SERVICE_NAME}.service"; then
    echo "  Warning: service did not start yet (check credentials/config). It is still enabled for boot:"
    echo "    journalctl -u ${SERVICE_NAME} -n 50 --no-pager"
fi

echo ""
echo -e "${GOLD}=========================================${RESET}"
echo -e "${GOLD}  Installation complete!${RESET}"
echo -e "${GOLD}=========================================${RESET}"
echo ""
echo "  The bot is enabled to start at boot via systemd (headless, no desktop autostart)."
echo ""
echo -e "${GOLD}  Service commands:${RESET}"
echo "    sudo systemctl status ${SERVICE_NAME}     # is it running?"
echo "    sudo systemctl restart ${SERVICE_NAME}    # restart after code/config changes"
echo "    sudo systemctl disable --now ${SERVICE_NAME}   # stop and disable boot start"
echo "    journalctl -u ${SERVICE_NAME} -f        # follow logs"
echo ""
echo -e "${GOLD}  Manual run (same as the service):${RESET}"
echo "    cd $SCRIPT_DIR && .venv/bin/python main.py"
echo "    # or: $SCRIPT_DIR/launcher.sh"
echo ""
