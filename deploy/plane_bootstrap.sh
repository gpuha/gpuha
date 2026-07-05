#!/usr/bin/env bash
# GPU HA plane bootstrap — run as root on gpuha-plane (Ubuntu 24.04).
# Installs deps + JupyterLab as a systemd service (token auth, 0.0.0.0:8888).
set -euo pipefail
export DEBIAN_FRONTEND=noninteractive
apt-get update -y
apt-get install -y git python3-venv python3-pip curl
python3 -m venv /opt/jl
/opt/jl/bin/pip install -q --upgrade pip
/opt/jl/bin/pip install -q jupyterlab
TOKEN=$(python3 -c 'import secrets;print(secrets.token_hex(24))')
install -d -m 755 /root/gpuha-workbench
cat >/etc/systemd/system/jupyterlab.service <<UNIT
[Unit]
Description=GPU HA JupyterLab workbench
After=network.target
[Service]
Type=simple
User=root
WorkingDirectory=/root/gpuha-workbench
ExecStart=/opt/jl/bin/jupyter lab --ip=0.0.0.0 --port=8888 --no-browser --allow-root --ServerApp.token=${TOKEN} --ServerApp.root_dir=/root/gpuha-workbench
Restart=always
RestartSec=3
[Install]
WantedBy=multi-user.target
UNIT
systemctl daemon-reload
systemctl enable --now jupyterlab.service
sleep 3
IP=$(curl -s ifconfig.me || echo PLANE_IP)
echo "=== JUPYTERLAB READY ==="
echo "URL:   http://${IP}:8888/lab?token=${TOKEN}"
echo "TOKEN: ${TOKEN}"
echo "(Cloud Firewall must allow TCP/8888 from your IP — see deploy/firewall_checklist.md)"
