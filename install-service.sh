#!/bin/bash
set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SERVICE_FILE="$SCRIPT_DIR/prisma-ui.service"

if [ ! -f "$SERVICE_FILE" ]; then
  echo "Error: prisma-ui.service not found in $SCRIPT_DIR"
  exit 1
fi

echo "Installing Prisma UI service..."
echo "Make sure you've edited prisma-ui.service with your username and paths first!"
echo ""

sudo cp "$SERVICE_FILE" /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable prisma-ui
sudo systemctl start prisma-ui
sudo systemctl status prisma-ui
