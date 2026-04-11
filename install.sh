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
#   - Prepares launcher.sh (optional helper; no GUI autostart)
#
# For headless operation, run main.py via systemd, cron, or your process
# supervisor — this installer does not open a terminal on login.
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
echo -e "${GOLD}[1/3] Installing system dependencies...${RESET}"
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
    echo -e "${GOLD}[2/3] Virtual environment already exists at $VENV_DIR, recreating...${RESET}"
    rm -rf "$VENV_DIR"
fi
echo ""
echo -e "${GOLD}[2/3] Creating virtual environment...${RESET}"
python3 -m venv "$VENV_DIR"

# Activate and install Python packages
echo ""
echo -e "${GOLD}[3/3] Installing Python dependencies...${RESET}"
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

echo ""
echo -e "${GOLD}=========================================${RESET}"
echo -e "${GOLD}  Installation complete!${RESET}"
echo -e "${GOLD}=========================================${RESET}"
echo ""
echo "  No GUI terminal autostart is configured (headless-friendly)."
echo ""
echo -e "${GOLD}  To run the bot:${RESET}"
echo "    cd $SCRIPT_DIR && .venv/bin/python main.py"
echo "    # or: $SCRIPT_DIR/launcher.sh"
echo ""
echo -e "${GOLD}  Headless:${RESET} use a systemd unit, cron, or your supervisor to run the command above."
echo ""
