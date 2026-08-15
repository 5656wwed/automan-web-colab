#!/bin/bash
set -e
echo "Stopping service..."
sudo systemctl stop automan-web 2>/dev/null || true
sudo systemctl disable automan-web 2>/dev/null || true
echo "Removing systemd unit..."
sudo rm -f /etc/systemd/system/automan-web.service
sudo systemctl daemon-reload
echo "Removing nginx config..."
sudo rm -f /etc/nginx/sites-enabled/automan-web /etc/nginx/sites-available/automan-web
sudo systemctl reload nginx 2>/dev/null || true
echo "Removing files..."
rm -rf /home/ubuntu/automan-web
echo "Done."
