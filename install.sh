#!/bin/bash
set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$SCRIPT_DIR"

# ---------------------------------------------------------------------------
# Configurable: change this to use a different service/device name.
# Use lowercase with hyphens for SERVICE_NAME (e.g. daq, launch-controller) - no spaces.
# Optionally set DISPLAY_NAME for banner text (e.g. "Launch Controller").
# Updates the systemd service name, service file, and all systemctl commands.
#
# This script also:
#   - Creates a Python virtual environment (.venv)
#   - Installs all libraries from requirements.txt
#   - Configures desktop autostart: a terminal window opens on login and runs main.py
#   - Requires: Raspberry Pi OS with Desktop, and Desktop Autologin enabled
# ---------------------------------------------------------------------------
SERVICE_NAME="${SERVICE_NAME:-trader}"
DISPLAY_NAME="${DISPLAY_NAME:-Kalshi Trading Bot}"

# ANSI colors: purple for logo, gold for highlights
PURPLE='\033[35m'
GOLD='\033[33m'
RESET='\033[0m'

# Logo (purple)
echo -e "${PURPLE}"
echo "#######        ########                       ######                     ######               ##### 
#######      ########                         ######                     ######              ###### 
#######    ########                           ######                     ######               ####  
#######   ########           ##########       ######     ##########      ######  #######     ###### 
####### ########          ################    ######   ###############   #################   ###### 
###############          #######   ########   ######  ######    #######  ##################  ###### 
################         #####       ######   ######  ######      #####  #######     ####### ###### 
#################          ################   ######  ###############    ######       ###### ###### 
########  ########       ##################   ######    ###############  ######       ###### ###### 
#######     ########    #######      ######   ###### #####       ####### ######       ###### ###### 
#######      ########   #######    ########## ###### #######     ######  ######       ###### ###### 
#######        #######   #################### ######  #################  ######       ###### ###### 
#######         ########   #########  ####### ######    ############     ######       ###### ###### "
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
    lxterminal \
    wmctrl \
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

# Disable systemd service if it exists (we use desktop autostart instead)
echo ""
echo -e "${GOLD}[4/4] Configuring desktop autostart...${RESET}"
if systemctl is-enabled "${SERVICE_NAME}.service" 2>/dev/null; then
    echo "  Disabling systemd service (using desktop autostart instead)..."
    sudo systemctl disable "${SERVICE_NAME}.service" 2>/dev/null || true
fi

# Install desktop autostart: terminal opens on login and runs the bot
AUTOSTART_DIR="$HOME/.config/autostart"
mkdir -p "$AUTOSTART_DIR"
AUTOSTART_FILE="$AUTOSTART_DIR/kalshi-trader.desktop"
# Use launcher.sh so it uses the venv and runs main.py
# Launch lxterminal with a title, then maximize it after a short delay
cat > "$AUTOSTART_FILE" <<EOF
[Desktop Entry]
Type=Application
Name=${DISPLAY_NAME}
Comment=Run ${DISPLAY_NAME} in a terminal window
Exec=bash -c 'lxterminal -t "${DISPLAY_NAME}" -e "cd $SCRIPT_DIR && $SCRIPT_DIR/launcher.sh; exec bash" & sleep 2 && wmctrl -r "${DISPLAY_NAME}" -b add,maximized_vert,maximized_horz'
X-GNOME-Autostart-enabled=true
EOF
echo "  Created $AUTOSTART_FILE"

echo ""
echo -e "${GOLD}=========================================${RESET}"
echo -e "${GOLD}  Installation complete!${RESET}"
echo -e "${GOLD}=========================================${RESET}"
echo ""
echo "  On desktop login, a terminal will open and run ${DISPLAY_NAME}."
echo ""
echo -e "${GOLD}  Requirements:${RESET}"
echo "    - Raspberry Pi OS with Desktop"
echo "    - Desktop Autologin enabled (raspi-config → Boot → Desktop Autologin)"
echo ""
echo -e "${GOLD}  To run manually:${RESET}"
echo "    cd $SCRIPT_DIR && .venv/bin/python main.py"
echo ""