# gpuha-plane — Linode Cloud Firewall checklist (default DENY inbound)
Attach a Cloud Firewall to the plane; inbound rules (all others denied):
| Proto | Port | Source | Purpose |
|-------|------|--------|---------|
| TCP | 22 | <SCOTT_IP>/32 | SSH (LISH remains OOB fallback) |
| TCP | 8888 | <SCOTT_IP>/32 | JupyterLab workbench |
| UDP | 5353 | <SCOTT_IP>/32 | DNS-drill queries to tier1 |
| UDP | 5106 | <EMITTER_IP>/32 | telemetry — **open per-drill only, then remove** |
Outbound: allow all. Default inbound policy: DROP.
