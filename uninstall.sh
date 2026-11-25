#!/usr/bin/env bash
# Keep the checkout, database and sessions for backup/reinstallation.
set -euo pipefail
if [[ ${EUID} -ne 0 ]]; then
    echo "Run with sudo."
    exit 1
fi
systemctl disable --now pg-toolbox.service
rm -f /etc/systemd/system/pg-toolbox.service
systemctl daemon-reload
printf 'Service removed. Your installation directory and database were preserved.\n'
