#!/bin/bash
set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SERVICE_FILE="$SCRIPT_DIR/open-clank.service"

if [ ! -f "$SERVICE_FILE" ]; then
  echo "Error: open-clank.service not found in $SCRIPT_DIR"
  exit 1
fi

echo "Installing Open Clank service..."
echo "Make sure you've edited open-clank.service with your username and paths first!"
echo ""

sudo cp "$SERVICE_FILE" /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable open-clank
sudo systemctl start open-clank
sudo systemctl status open-clank
