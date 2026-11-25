#!/usr/bin/env bash
set -euo pipefail
systemctl restart pg-toolbox.service
systemctl --no-pager --full status pg-toolbox.service
