# GPU HA — Architecture (as built)

**Date:** 2026-07-04 · Companion docs: TARGET_STATE.md (roadmap), WHITEPAPER.md (narrative), OPEN_CORE.md (open/closed boundary). Everything below is running or proven code unless marked.

---

## 1. Runtime data plane — a request's-eye view

```
client (OpenAI SDK, one base URL: api.gpuha.com)
   │
   │ (1) resolve
   ▼
Tier-1 DNS ─ tier1_dns.py · plane · udp/5353
   │   answer = every pool with ≥1 fresh node (10s pool window)
   │   ALL pools dark → answer = WHALE IP   [FAILSAFE→WHALE]
   │
   │ (2) connect to the chosen pool's router :9000
   ▼
Tier-2 router (one per pool) ─ OpenAI-compatible
   │   selector: freshness gate (3s node window) → weighted score
   │   (TTFT + VRAM headroom; price tiebreak) → circuit breaker →
   │   anti-herding (squared-score sampling)
   │   first-token latch: pre-token failure = silent retry next-best;
   │   post-token failure = clean truncation, never a fake resume
   │
   │ (3) proxy (chunked + Content-Length framings)
   ▼
vLLM workers :8000 (stock, unmodified vendor software)
   └─ vllm_telemetry_shim.py scrapes /metrics →

TelemetryFrame v1 (UDP, content-free: identity + seq + metrics, never
a prompt or token) fans out to BOTH consumers — same ingest class:
   → udp/5006  pool router (local)      node liveness, per-request pick
   → udp/5106  plane tier1 (internet)   pool liveness, evacuation

whale.py ─ plane :8080 ─ last resort. Precomputed OpenAI-shaped
responses (200 graceful completion, model="gpuha-degraded", or 503 +
Retry-After for SDK self-heal). Serves only when DNS has failed
everything to it. Dumbest thing in the fleet by design: no telemetry,
no state, no dependencies.
```

Liveness is the consumer's judgment in both tiers: **silence = death.**
No worker ever asserts health; each consumer ages nodes/pools out by
frame arrival against its own clock.

## 2. Infrastructure topology — what exists where

```
ALWAYS-ON PLANE — Linode 2GB · 203.0.113.10 · ~$12/mo (the only
┌──────────────────────────────────────────────┐   standing compute)
│ tier1_dns.py   udp/5353 DNS · udp/5106 ingest │  [systemd, boot]
│ whale.py       :8080 · --workers 2            │  [systemd, boot]
│ canonical git clone  ~/gpuha-workbench/gpuha  │  (GitHub = durability)
│ JupyterLab :8888 (workbench, replaced Cloud   │
│   Shell) · file-server :8090 (bootstrap fetch)│
│ orchestrator CLI (up/down/reap) + run-state   │
│ deadman.sh — independent reap-all timer       │
└──────────────────────────────────────────────┘
        ▲ TelemetryFrames udp/5106 (firewall-scoped per-worker /32)
  ┌─────┴──────────┬───────────────────┬──────────────────┐
  LAMBDA pool      RUNPOD pool         GCP pool           EPHEMERAL:
  2× GPU on        2× L4 pods          1× L4              exist only
  SEPARATE         (direct-TCP port    (zone fallback     during runs;
  instances,       maps; NAT: egress   us-east1-b→c→d;    $0 idle by
  router-head      IP ≠ reachable IP;  DLVM image)        delete/
  on A fronting    baked venv volume =                    terminate
  A+B              deliberate warm-tier idle cost)
```

Production TODO (P1, logged not built): **whale moves off-plane** —
co-location fate-shares the last resort with tier1; a whale flood can
starve the same box's DNS/UDP. Dedicated host or anycast IP; interim a
$5 Nanode.

## 3. Orchestrator lifecycle — `gpuha up / down / reap`

```
topology.yaml (pools, workers, quorum, prefs)
   │  parse → persist run-state JSON  ◄── BEFORE any create
   ▼
ACQUIRE   per-pool provider adapter: create N instances/pods,
   │      name-tag gpuha-managed--<run-id>--<i>,
   │      region/zone fallback; stockout = logged event, not error;
   │      below min_pools quorum → abort AND tear down partial fabric
   ▼
BOOTSTRAP ssh (setsid, never nohup): baked venv (warm) or
   │      pin-detect + pip (cold); fetch code from plane :8090;
   │      launch vLLM + router + shim (fan-out --dest local + plane)
   ▼
WIRE      write tier1-pools.map → restart tier1;
   │      plane firewall: udp/5106 ← worker egress /32 only
   ▼
VERIFY    poll until warm (dig shows pool + real streamed completion
   │      through the pool router) — cold-start is a measured,
   │      budgeted, first-class cost (20-min cold pip = DR, not HA)
   ▼
UP ───────────── drills / demo ─────────────┐
                                            ▼
DOWN      per-provider-correct destroy (terminate/delete per the
   │      lifecycle matrix), unwire pool map, verify gone
   ▼
REAP --all  per-provider name-prefix sweep; each provider wrapped in
            its own try/except — a broken provider cannot abort the
            sweep ("the suspenders can't depend on the belt")
DEADMAN     independent timer → reap --all regardless of orchestrator
            state — the backstop that runs even if everything wedges
```

Provider lifecycle matrix (the scar tissue, encoded): Linode plane =
never touched · GCP = delete (stop leaves disk pennies) · Lambda =
TERMINATE only (no stop state; disk destroyed; bills until terminated)
· RunPod = DELETE pods (stopped pods may be unrestartable), volumes
bill while existing.

## 4. Failure ladder — the contracts, smallest to largest

```
worker slow            → stays eligible; TTFT score demotes it
                         (slow ≠ dead; scrape timeout ≪ freshness window)
worker silent > 3s     → router evicts node          (silence = death)
dies BEFORE 1st token  → silent retry next-best; client sees nothing
dies AFTER 1st token   → clean truncation; no fake resume (KV honesty)
pool silent > 10s      → tier1 drops pool from the DNS answer
ALL pools dark         → tier1 answers WHALE IP   [FAILSAFE→WHALE]
whale                  → 200 graceful completion (model=gpuha-degraded)
                         or 503 + Retry-After (SDK auto-retry self-heal)
residual (honest)      → clients holding cached DNS (20–60s resolver
                         floors) hit dead pool IPs until TTL expiry;
                         anycast whale = deferred production mitigation
```

## 5. In-flight topology change (step 3 of current plan)

```
C′-a as proven (co-resident):        Router-head target (new wiring):
┌── one A10 ────────────┐            instance A              instance B
│ router :9000          │            ┌─ router :9000 ──┐     ┌─────────┐
│ vLLM w1 ·· vLLM w2    │            │ vLLM w1 :8000   │◄───►│ vLLM w2 │
└───────────────────────┘            └─────────────────┘     │  :8000  │
= process-level failover             = host-level failover   └─────────┘
                                     B's :8000 firewall-scoped to A's
                                     IP ONLY — the router-head is the
                                     sole legitimate client of a
                                     sibling's vLLM port.
```

## 6. Ports & windows quick reference

| Thing | Value |
|---|---|
| tier1 DNS | udp/5353 (POC; port 53 + production wiring = D2) |
| tier1 telemetry ingest | udp/5106 (plane) |
| router-local telemetry | udp/5006 |
| pool router | :9000 |
| vLLM | :8000 (RunPod fake-worker collisions → 8011+) |
| whale | :8080 |
| JupyterLab / file-server | :8888 / :8090 (plane) |
| node freshness / pool window / breaker | 3s / 10s / 5s |
| verify budgets | stub ~210s · cold GPU 900s+ · warm-baked TBD (#73) |

## 7. Diagrams (rendered)

Rendered SVG companions to the ASCII above — the **presentation layer**. The
ASCII in §1–§5 stays the diffable source-of-truth; refresh these when it
changes. Index: [`diagrams/README.md`](diagrams/README.md).

| Diagram | Renders | File |
|---|---|---|
| System architecture | §1 data plane + §2 topology | [`diagrams/d1_architecture.svg`](diagrams/d1_architecture.svg) |
| Failover demo | per-request routing + Served-By flip | [`diagrams/d2_demo.svg`](diagrams/d2_demo.svg) |
| Traffic harness (Phase H) | client-traffic / stream-safe drain | [`diagrams/d3_harness.svg`](diagrams/d3_harness.svg) |
| Orchestrator lifecycle | §3 up / down / reap | [`diagrams/d4_orchestrator.svg`](diagrams/d4_orchestrator.svg) |
| Split-brain resolution | §4 failure ladder (silence = death) | [`diagrams/d5_splitbrain.svg`](diagrams/d5_splitbrain.svg) |
| Open-core boundary | open surface vs. CLOSED plane (OPEN_CORE.md) | [`diagrams/d6_opencore.svg`](diagrams/d6_opencore.svg) |
