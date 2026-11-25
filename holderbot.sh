#!/usr/bin/env bash
# Install or update this fork; preserves configuration and local templates.
set -euo pipefail

if [[ ${EUID} -ne 0 ]]; then
    echo "Run this installer with sudo."
    exit 1
fi
install_dir=${PG_INSTALL_DIR:-/opt/pg-toolbox}
repo_url=${PG_REPOSITORY:-https://github.com/DenTeeR/PG-ToolBox.git}
branch=${PG_BRANCH:-main}
python_bin=${PG_PYTHON:-python3}
if [[ ! $install_dir =~ ^/[A-Za-z0-9_./-]+$ || $install_dir == / ]]; then
    echo "PG_INSTALL_DIR must be an absolute directory path without spaces."
    exit 1
fi
command -v systemctl >/dev/null || { echo "A systemd-based Debian/Ubuntu server is required."; exit 1; }
apt-get update
apt-get install -y python3 python3-venv git ca-certificates

if [[ -d $install_dir/.git ]]; then
    if [[ -n $(git -c safe.directory="$install_dir" -C "$install_dir" status --porcelain) ]]; then
        echo "Local source changes found in $install_dir. Commit or archive them before updating."
        exit 1
    fi
    git -c safe.directory="$install_dir" -C "$install_dir" fetch origin "$branch"
    git -c safe.directory="$install_dir" -C "$install_dir" merge --ff-only FETCH_HEAD
elif [[ -e $install_dir ]]; then
    echo "$install_dir already exists and is not a Git checkout. Choose another PG_INSTALL_DIR."
    exit 1
else
    git clone --branch "$branch" --single-branch "$repo_url" "$install_dir"
fi
if systemctl cat pg-toolbox.service >/dev/null 2>&1; then
    systemctl stop pg-toolbox.service
fi
"$python_bin" -c 'import sys; sys.exit(0 if (3, 10) <= sys.version_info[:2] <= (3, 12) else "Use Python 3.10–3.12; set PG_PYTHON to its executable.")'
"$python_bin" -m venv "$install_dir/.venv"
"$install_dir/.venv/bin/pip" install -r "$install_dir/requirements.txt"
cd "$install_dir"
# Prompts remain usable when the installer is fetched through curl.
"$install_dir/.venv/bin/python" setup.py </dev/tty
if ! id pg-toolbox >/dev/null 2>&1; then
    useradd --system --home-dir "$install_dir" --shell /usr/sbin/nologin pg-toolbox
fi
chown -R pg-toolbox:pg-toolbox "$install_dir"
cat > /etc/systemd/system/pg-toolbox.service <<EOF
[Unit]
Description=PG-ToolBox Telegram bot for PasarGuard
Wants=network-online.target
After=network-online.target

[Service]
Type=simple
User=pg-toolbox
Group=pg-toolbox
WorkingDirectory=$install_dir
ExecStart=$install_dir/.venv/bin/python -u $install_dir/holder.py
Restart=on-failure
RestartSec=10
UMask=0077
EnvironmentFile=-/etc/pg-toolbox.env
NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=strict
ProtectHome=true
ReadWritePaths=$install_dir

[Install]
WantedBy=multi-user.target
EOF
systemctl daemon-reload
systemctl enable --now pg-toolbox.service
systemctl --no-pager --full status pg-toolbox.service
printf '\nOpen your Telegram bot and send /start. Logs: journalctl -u pg-toolbox -f\n'
