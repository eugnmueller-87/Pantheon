#!/bin/bash
# Install ZEUS systemd services on the server.
# Run as root from the ZEUS repo root:
#   bash infra/hetzner/install-services.sh
set -euo pipefail

ZEUS_DIR="/opt/pantheon"

cp infra/hetzner/zeus.service /etc/systemd/system/zeus.service
cp infra/hetzner/zeus-dashboard.service /etc/systemd/system/zeus-dashboard.service

systemctl daemon-reload
systemctl enable zeus zeus-dashboard
systemctl restart zeus zeus-dashboard

echo "Done. Status:"
systemctl status zeus zeus-dashboard --no-pager
