#!/bin/bash
# GPU HA — Hidden Master (coredns-edge) build + harden recipe
# RECOVERED via LISH from gpuha-coredns-edge (74.207.243.66) 2026-07-03.
# The two heredoc'd Go sources are captured verbatim as siblings: gpuha.go, setup.go.
# Lines 1-13 (Go toolchain download) and exact go-build invocation summarized from console capture.
set -e

# 1-2. Install Go 1.22.4
# (download go1.22.4.linux-amd64.tar.gz, then:)
sudo rm -rf /usr/local/go && sudo tar -C /usr/local -xzf go1.22.4.linux-amd64.tar.gz
rm -f go1.22.4.linux-amd64.tar.gz
export PATH=$PATH:/usr/local/go/bin
if ! grep -q "/usr/local/go/bin" ~/.bashrc; then
    echo 'export PATH=$PATH:/usr/local/go/bin' >> ~/.bashrc
fi

# 3. CoreDNS Source Allocation
if [ ! -d "/root/coredns" ]; then
    echo "--- Allocating CoreDNS Code Base ---"
    git clone https://github.com/coredns/coredns.git /root/coredns
fi

# 4. Inject Custom Plugin Package (gpuha.go — see sibling gpuha.go, 177 lines)
mkdir -p /root/coredns/plugin/gpuha
cd /root/coredns/plugin/gpuha
cat << 'GOEOF' > gpuha.go
# ... body captured verbatim in legacy/gpuha.go ...
GOEOF

# 5. Inject Setup Hook (setup.go — see sibling setup.go, Caddy plugin registration)
echo "--- Compiling 'setup.go' Plugin Wrapper ---"
cat << 'GOEOF' > setup.go
# ... body captured verbatim in legacy/setup.go ...
GOEOF

# 6. Bind Manifest and Compile
echo "--- Reconfiguring Compilation Pipeline Manifests ---"
cd /root/coredns
if ! grep -q "gpuha:gpuha" plugin.cfg; then
    sed -i '/^hosts:hosts/i gpuha:gpuha' plugin.cfg
fi
# (go generate && go build -o /root/coredns/coredns ; ExecStart uses /root/coredns/coredns)

# 7. Baseline Corefile + zone stub (Corefile + api.gpuha.com.zone captured as siblings)
if [ ! -f "/root/coredns/api.gpuha.com.zone" ]; then
    echo "--- Creating Baseline Zone File Stub ---"
    cat << 'ZEOF' > /root/coredns/api.gpuha.com.zone
$TTL 0
@   IN  SOA ns1.gpuha.com. admin.gpuha.com. (
        1000000000 ; Serial
        300        ; Refresh
        60         ; Retry
        1209600    ; Expire
        0          ; Negative Cache TTL
)
@   IN  NS  ns1.gpuha.com.
ns1 IN  A   127.0.0.1
@   IN  A   127.0.0.1
ZEOF
fi

# 8. Remediate Port 53 Operating System Conflicts
echo "--- Neutralizing systemd-resolved Port 53 Interferences ---"
if systemctl is-active --quiet systemd-resolved || [ -f /run/systemd/resolve/stub-resolv.conf ]; then
    # (disable/mask systemd-resolved, free port 53)
    :
fi

# 9. systemd unit (see sibling gpuha-coredns.service)
sudo systemctl daemon-reload
sudo systemctl enable gpuha-coredns.service
# Force-kill any lingering port ties before systemd spin-up
sudo fuser -k 5006/udp || true
sudo systemctl restart gpuha-coredns.service

# 10. Automated Local Firewall Hardening Layer  (THIS is why Step-0 probes saw it "dead")
echo "--- Initiating System Firelocking Matrix ---"
sudo ufw allow 22/tcp || true
sudo ufw allow from 69.164.215.134 to any port 5006 proto udp    # aggregator -> edge state feed
sudo ufw allow from 104.237.137.0/24 to any port 53
sudo ufw allow from 45.79.109.0/24  to any port 53
sudo ufw allow from 74.207.225.0/24 to any port 53
sudo ufw allow from 143.42.7.0/24   to any port 53
sudo ufw allow from 109.74.194.0/24 to any port 53
sudo ufw default deny incoming
sudo ufw default allow outgoing
sudo ufw --force enable

echo "=== [GPUHA BUILD] SUCCESS! Production Master is Operational, Isolated, and Anycast-Ready ==="
