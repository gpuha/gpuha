# legacy/ — dnshat-lineage recovery (Phase L, 2026-07-03)

Recovered out-of-band via LISH/Weblish (boxes have no public SSH — ufw-hardened, not dead).
These are **text donors** for the writeup and for D2 (which rebuilds fresh on a clean host).

## Hidden master — gpuha-coredns-edge (74.207.243.66, Linode 8GB, Fremont)
- **gpuha.go** — the custom CoreDNS plugin (177 lines). Listens UDP/5006 for the aggregator's
  cluster-state snapshots; `calculateBestRoute()` picks the lowest-TTFT node with
  vram_saturation<=85% & spot_price<=$2.00 (else fallback tier); 2.5s debounce; on commit it
  `rewriteZoneFile()` (serial=epoch) and fires RFC-1996 NOTIFY to 5 anycast slaves.
- **setup.go** — Caddy plugin registration (`init()`/`setup()`), inserted `gpuha:gpuha` into plugin.cfg.
- **Corefile**, **api.gpuha.com.zone**, **gpuha-coredns.service**, **build.sh** (full build+harden recipe).
- Why Step-0 probes saw it "dead": build.sh §10 ufw hardening — allow 22, allow 5006/udp ONLY from the
  aggregator (69.164.215.134), allow 53 only from 5 anycast /24s, default-deny incoming. Alive, just closed.

## Aggregator — gpuha-telemetry-aggregator (69.164.215.134, Nanode, Newark)
- **aggregator/aggregator_daemon.py** (82 lines) + **aggregator/gpuha-aggregator.service**.
- Listens UDP/5005 for per-target JSON; every 1s rebroadcasts the whole `cluster_state` dict to the
  edge at 74.207.243.66:5006. Banner: "GPUHA CENTRAL TELEMETRY AGGREGATOR ENGINE v1.2".

## v0 contract (recovered) vs TelemetryFrame v1 (current) — writeup contrast
- **v0 inbound frame** (target -> aggregator:5005): `{ target_id, ttft_ms, vram_saturation_pct,
  spot_price_hr, timestamp }`. Aggregator maps timestamp->last_seen_epoch, adds source_ip, and
  **rebroadcasts a full cluster_state snapshot** to the edge each second.
- **v1 TelemetryFrame** (current): `{ node_id, backend, region, ts_unix, seq, vram_used_frac, ttft_ms,
  queue_depth, price_usd_hr, model, max_concurrency, v }` — per-node frames with **seq de-dup, a
  version field, and a freshness gate** (silence=death) consumed identically by both tiers.
- Deltas that mattered: v0 had no seq/version/per-frame freshness and shipped whole-map snapshots;
  v1 is per-node, versioned, de-duplicated, and drives BOTH the L7 router and Tier-1 DNS from one contract.

## Live-DNS note (kill context)
Hidden-master zone A record currently points api.gpuha.com -> 45.56.119.145 (dead lambda-target);
public api.gpuha.com (Linode DNS Manager) -> 45.79.114.52 (coreweave). Both are ghost infra; nothing
real depends on them. Follow-up after plane is up: repoint the DNS-Manager A record to the plane/whale.
