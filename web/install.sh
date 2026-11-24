#!/bin/sh
# Install the The Hub web board on Debian or Ubuntu. Run as root from this folder.
set -e
apt-get update -qq
apt-get install -y -qq python3-aiohttp
id deskhub >/dev/null 2>&1 || useradd --system --no-create-home --shell /usr/sbin/nologin deskhub
install -d /opt/deskhub-web
install -m 644 server.py index.html /opt/deskhub-web/
if [ ! -f /etc/deskhub-web.env ]; then
  tok=$(head -c 24 /dev/urandom | base64 | tr -dc 'A-Za-z0-9' | head -c 24)
  printf 'DESKHUB_TOKEN=%s\nDESKHUB_PORT=80\nDESKHUB_CONTROLS=1\n' "$tok" > /etc/deskhub-web.env
  chmod 640 /etc/deskhub-web.env; chgrp deskhub /etc/deskhub-web.env
fi
install -m 644 deskhub-web.service /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now deskhub-web
echo "Companion token: $(sed -n 's/^DESKHUB_TOKEN=//p' /etc/deskhub-web.env)"
echo "Page code:       $(sed -n 's/^DESKHUB_PIN=//p' /etc/deskhub-web.env)"
