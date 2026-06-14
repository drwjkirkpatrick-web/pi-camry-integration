#!/bin/bash
# scripts/install.sh
# ─────────────────
# One-shot installer for Pi-Camry integration on Raspberry Pi 5
# Run as: curl -sSL ... | sudo bash   (or clone and run locally)

set -euo pipefail

REPO_URL="https://github.com/drwjkirkpatrick-web/pi-camry-integration.git"
INSTALL_DIR="/opt/pi-camry-integration"
CONFIG_DIR="/etc/pi-camry"
USER="pi"

log() { echo "[pi-camry] $*"; }

# ── 1. System dependencies ──
log "Installing system dependencies..."
apt-get update
apt-get install -y \
    python3-pip python3-venv python3-dev \
    libcamera-dev libgpiod-dev i2c-tools \
    espeak-ng ffmpeg alsa-utils \
    git curl \
    libcap-dev \
    libatlas-base-dev

# Enable I2C, SPI, camera
log "Enabling hardware interfaces..."
raspi-config nonint do_i2c 0
raspi-config nonint do_spi 0
raspi-config nonint do_camera 0

# ── 2. Clone repo ──
if [[ -d "$INSTALL_DIR" ]]; then
    log "Updating existing install..."
    cd "$INSTALL_DIR"
    git pull origin main
else
    log "Cloning repository..."
    git clone "$REPO_URL" "$INSTALL_DIR"
    cd "$INSTALL_DIR"
fi

# ── 3. Python environment ──
log "Setting up Python environment..."
python3 -m venv .venv
source .venv/bin/activate
pip install --upgrade pip uv
uv pip install -e ".[dev]"

# ── 4. faster-whisper model download ──
log "Downloading Whisper model (tiny)..."
python3 -c "from faster_whisper import WhisperModel; WhisperModel('tiny.en', device='cpu', compute_type='int8')" || true

# ── 5. Create config directory ──
log "Creating config directory..."
mkdir -p "$CONFIG_DIR"
chmod 700 "$CONFIG_DIR"

if [[ ! -f "$CONFIG_DIR/camry.yaml" ]]; then
    cp configs/camry.example.yaml "$CONFIG_DIR/camry.yaml"
    log "Created $CONFIG_DIR/camry.yaml — EDIT THIS FILE"
fi

# ── 6. M.2 NVMe setup (if not already mounted) ──
if ! mountpoint -q /mnt/nvme0n1p2 2>/dev/null; then
    log "M.2 NVMe not mounted at /mnt/nvme0n1p2"
    log "Please partition and mount your NVMe drive, then rerun."
    log "See docs/storage-setup.md for instructions."
fi

# ── 7. Systemd service ──
log "Installing systemd service..."
cp scripts/camry-daemon.service /etc/systemd/system/
sed -i "s|/home/pi/pi-camry-integration|$INSTALL_DIR|g" /etc/systemd/system/camry-daemon.service
sed -i "s|/home/pi/.local/bin/uv|$INSTALL_DIR/.venv/bin/uv|g" /etc/systemd/system/camry-daemon.service
systemctl daemon-reload
systemctl enable camry-daemon

# ── 8. Permissions ──
log "Setting permissions..."
chown -R "$USER:$USER" "$INSTALL_DIR"
chown -R "$USER:$USER" "$CONFIG_DIR"

# Add user to gpio, i2c, spi groups
usermod -aG gpio,i2c,spi,audio "$USER" || true

# ── 9. Bench test ──
log ""
log "========================================"
log "Installation complete!"
log "========================================"
log ""
log "Next steps:"
log "  1. Edit config:   sudo nano $CONFIG_DIR/camry.yaml"
log "  2. Set Telegram:  sudo nano $CONFIG_DIR/environment"
log "  3. Run bench:     sudo $INSTALL_DIR/.venv/bin/python scripts/bench_test.py --all"
log "  4. Start daemon:  sudo systemctl start camry-daemon"
log "  5. View logs:     sudo journalctl -u camry-daemon -f"
log ""
log "Config:     $CONFIG_DIR/camry.yaml"
log "Install:    $INSTALL_DIR"
log "Service:    systemctl {start|stop|status} camry-daemon"
log ""
