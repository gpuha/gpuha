# BUILD_YOUR_OWN.md -- HA/DR for LLM Inference: A Practitioner's Manual

> **What this is:** a working reference implementation and a build guide for high-availability
> and disaster-recovery of LLM inference workloads across clouds. Clone it, run the drills,
> understand the boundaries, then build your own HA/DR layer on top for your inference apps.
>
> **What this is NOT:** a product, a SaaS, or a managed service. It's an open reference --
> Apache-2.0, prior art, yours to fork. Raising tide, all boats.

**Author:** Scott McDonald - **Status:** reference implementation, drilled on real GPUs across three clouds.

---

## How to read this manual (3 audiences, 3 entry points)

- **"I just need HA for my vLLM fleet in one region"** -> Section 4 (the L7 router). Clone, run, done. Skip the DNS tier.
- **"I need cross-cloud / cross-region DR"** -> the whole thing. Section 3 first (the thesis), then 4-7.
- **"My agent is building this for me"** -> Section 9 (the reproducible spec: exact commands, exact expected outputs, the drill matrix as acceptance tests).

---

## 1. The problem, stated honestly

LLM inference is becoming tier-1 infrastructure; its availability story is immature. People conflate
two failure classes: **coarse/sticky** (a whole pool/region/cloud dies) versus **fast/per-request**
(which specific worker serves THIS request, and what happens if it dies mid-stream). The core insight
the whole design rests on: **these two classes want two different tools.** Most setups pick one and
paper over the other.

DNS-based failover suited websites because the failure was coarse and sticky. Streaming inference
breaks that in two ways. First, **the connection outlives the decision** -- a streaming completion
holds a socket open for 5-90 seconds, so DNS is out of the loop the moment the socket opens; no TTL is
low enough to re-route a request already in flight. Second, **the decision is fast-moving and
per-request** -- "which worker has the lowest time-to-first-token and the most VRAM headroom right
now" changes second to second, and public resolvers override TTL 0 with a 20-60s cache floor.

## 2. Prerequisites

- A cheap always-on box (the control plane / "plane") -- ~$5-12/mo (we used a 2GB Linode).
- Accounts on 1+ GPU providers (we cover Lambda, RunPod, GCP; the pattern generalizes).
- vLLM (stock, unmodified -- a design goal), Python 3.11+, stdlib only for the data plane.
- Comfort with DNS basics, HTTP/streaming, UDP, systemd, SSH.

**GPU-worker pin table** (a stock `pip install vllm` pulls a CUDA-13 build that won't run on
CUDA-12.4-era drivers -- see the landmines in Section 9):

```bash
pip install "torch==2.8.0+cu128" "torchvision==0.23.0+cu128" "torchaudio==2.8.0+cu128" \\
            "vllm" "transformers<5" hf_transfer \\
            --extra-index-url https://download.pytorch.org/whl/cu128
```

The **data plane** (router, selection, tier1, whale, shim, fake_worker) is **stdlib-only** -- no pip
for the no-GPU demo below.

## 3. The thesis: where DNS's reach ends (READ THIS BEFORE YOU BUILD)

- **DNS (Tier 1) owns coarse pool evacuation** -- a region/cloud losing capacity, telemetry going
  dark, cross-cloud disaster failover. Unbeatable at this: coarse, sticky, seconds-to-minute.
- **An L7 hop (Tier 2) owns the per-request decision** -- which worker serves this request, and what
  happens if it dies mid-stream. DNS can't reach a request already in flight.

The discipline is knowing which failure you're solving. Full picture: `docs/diagrams/d1_architecture.svg`.

## 4. Tier 2 -- the L7 router (start here for single-region HA)

- **What it does:** one OpenAI-compatible endpoint; picks the best live worker per request; fails over silently before first token, fails fast after.
- **The first-token contract:** before first token = silently retry another worker; after first token = clean truncation, never a fake resume (KV-cache isn't shared; pretending to resume fabricates output). A one-way latch.
- **Liveness = silence.** No worker asserts health; the consumer judges by telemetry freshness (node gate = **3s**). A zombie (VRAM full, no serving process) can't fool it.
- **Components:** `router.py` (proxy + latch + dual framing), `selection.py` (freshness gate -> weighted score -> breaker -> anti-herding), `vllm_telemetry_shim.py` (scrapes stock vLLM /metrics -> UDP frame).

**Run it -- the no-GPU local demo (~2 min, stdlib only, zero spend).** `fake_worker.py` stands in for a vLLM+shim: it serves OpenAI-shaped streams AND emits the telemetry frame.

```bash
# 1) the router: serves on :9000, ingests telemetry on udp/5006, fronts two named backends
python router.py --port 9000 --telemetry-port 5006 \
    --backend gpuha-w1=127.0.0.1:8011 --backend gpuha-w2=127.0.0.1:8012 &

# 2) two fake workers -- --id MUST match the router backend name; --telemetry points at the router
python fake_worker.py --id gpuha-w1 --port 8011 --telemetry 127.0.0.1:5006 &
python fake_worker.py --id gpuha-w2 --port 8012 --telemetry 127.0.0.1:5006 &

# 3) send a request and see who served it
curl -s -D - -o /dev/null localhost:9000/v1/chat/completions -H 'Content-Type: application/json' \
    -d '{"model":"gpuha","messages":[{"role":"user","content":"hi"}],"max_tokens":8}' | grep -i x-gpuha-served-by
```

**Prove it -- intra-pool failover (Phase A / RP-P2).** Stream requests, then kill one worker mid-traffic:

```bash
while true; do curl -s -D - -o /dev/null localhost:9000/v1/chat/completions \
  -H 'Content-Type: application/json' \
  -d '{"model":"gpuha","messages":[{"role":"user","content":"hi"}],"max_tokens":8}' \
  | awk 'tolower($0) ~ /x-gpuha-served-by|http/'; sleep 0.3; done &
pkill -f 'fake_worker.py --id gpuha-w2'
```

**Expected (acceptance):** `X-GPUHA-Served-By` flips from a w1/w2 mix to **w1 only** within the 3s
freshness window; **every client request stays HTTP 200** (zero errors); `curl -s localhost:9000/__stats`
shows `failovers` incremented and `eligible` collapse `[w1,w2]` -> `[w1]`. On real GPUs: 88/88 client 200s,
`failovers=1`, w2 evicted.

## 5. Tier 1 -- the DNS tier (add this for cross-cloud/region DR)

- **What it does:** answers the set of pools with fresh telemetry; evacuates a pool whose telemetry goes dark (pool window = **10s**); fails to the whale when all pools die.
- **The shared telemetry contract:** ONE content-free frame drives both tiers -- same ingest code. The router consumes it per-request; the DNS tier consumes it for pool eviction. The load-bearing design decision.
- **Fail-open, never fail-empty:** all pools dark -> answer the whale IP, never NXDOMAIN, never stale-as-live.
- **Components:** `tier1_dns.py` (stdlib DNS responder + the shared `TelemetryIngest`).

```bash
python tier1_dns.py --dns-port 5353 --telem-port 5106 --fresh 10 \
    --pool gpuha-pool-a=<POOL_A_IP> --pool gpuha-pool-b=<POOL_B_IP> --failsafe-ip <WHALE_IP> &
# dynamic pools: --pool-file <path> is re-read on SIGHUP so the orchestrator can rewire live
# each worker's shim registers its pool with the plane:
python vllm_telemetry_shim.py --worker-url http://127.0.0.1:8000 --node-id gpuha-w1 \
    --dest 127.0.0.1:5006 --dest <PLANE_IP>:5106 --backend gpuha-pool-a --region us-east1 --model gpuha &
```

**Prove it** -- Phase D/D.5, queried with `dig`:

```bash
dig @127.0.0.1 -p 5353 api.gpuha.com +short   # baseline: all fresh pool IPs
# kill an entire pool's workers (telemetry stops), wait >10s:
dig @127.0.0.1 -p 5353 api.gpuha.com +short   # evacuate: dead pool IP dropped, survivor remains
# kill ALL pools, wait >10s:
dig @127.0.0.1 -p 5353 api.gpuha.com +short   # failsafe: single answer = the whale IP
```

**Expected:** dead pool's IP disappears within 10s, survivor remains; all-dark -> whale IP (never
empty). On real GPUs (RP-P3) we killed the RunPod pool and `dig` dropped it, leaving only Lambda;
all-dark returned the whale.

## 6. The whale -- graceful degradation when everything is dark

- When all pools die, clients get a valid OpenAI-shaped "please try again" completion (or a 503 + Retry-After the SDK auto-retries), **not** connection-refused.
- **Governing principle:** the whale is the dumbest thing in the fleet -- no state, no deps, precomputed responses. A last resort must share fate with nothing.

```bash
python whale.py --port 8080 --mode auto --model-name gpuha-degraded --retry-after 30 --workers 2 &
curl -s localhost:8080/v1/chat/completions -H 'Content-Type: application/json' \
    -d '{"model":"gpuha","messages":[{"role":"user","content":"hi"}],"max_tokens":8}'
# expect 200 with "model":"gpuha-degraded" and a graceful message.
# --mode error instead returns 503 + Retry-After for SDK self-heal.
```

**Honest residual:** clients holding a cached DNS answer (resolver TTL floors, 20-60s) still hit dead pool IPs until TTL expiry; the whale closes the post-TTL experience, not the cached window.

## 7. Orchestration -- pools up/down across clouds (the DR automation)

- **The loop:** `gpuha up <topology.yaml>` acquires capacity best-effort across providers, wires the fabric, verifies with a real completion; `gpuha down` tears to zero.
- **Teardown-first, or don't bother:** every provider's failure mode is a billing trap. State is persisted before create; `reap --all` catches orphans by provider-truth, not state-truth (the kill-test lesson). A deadman timer is the ultimate backstop.
- **Cold-start is a first-class cost:** a 20-min cold stand-up is DR, not HA. Budget it; parallelize.

**The provider lifecycle matrix (the scar tissue, encoded):**

| Provider | Teardown rule | Why |
|---|---|---|
| Linode (plane) | never touched | the always-on control plane |
| GCP | **delete** (not stop) | stopped instances bill for disk; stop/start also changes internal IPs |
| Lambda | **terminate only** | no stop state; billed per-minute until terminated; terminate destroys local disk (commit/push first) |
| RunPod | **delete pods** (not stop) | stopped pods may be unrestartable; volumes bill while they exist -- delete them too |

```bash
./gpuha up orchestrator/topologies/demo-lambda-rh.yaml   # acquire -> bootstrap -> wire -> verify
./gpuha down <run-id>                                    # per-provider-correct destroy + unwire
./gpuha reap --all                                       # name-prefix orphan sweep (the billing backstop)
```

**Expected:** `up` reaches `status: up` only after a real streamed completion through the pool router succeeds; `down` + `reap --all` returns provider APIs to **0 managed instances / $0**. Multi-pool bring-up is concurrent (~16.5s vs serial for two stub pools).

## 8. Building YOUR HA/DR solution on top of this

- **A DC is just a pool.** On-prem, internal DNS removes the TTL-floor problem -- DC-to-DC failover is STRONGER than on the public internet.
- **Adapt the topology spec** to your providers/regions/DCs; the pool abstraction is location-agnostic.
- **Blue/green and canary come free:** which pools the DNS tier advertises = coarse blue/green; router weights = canary; the whale = maintenance mode.
- **The DR-evidence angle:** the kill-drill methodology with captured evidence productizes as scheduled DR exercises with auditor-ready reports.

**Extension pattern:** write a provider adapter (see `orchestrator/adapters/`) implementing `acquire / bootstrap / verify / teardown / reap`; add a pool to the topology YAML; the two tiers pick it up unchanged. For NAT'd providers where the worker can't push UDP to the router, relocate the shim to the router host and scrape the worker (how the RunPod router-head works), and protect any internet-exposed worker port with `vllm --api-key` -- the port is public by design.

## 9. Reproducible spec (engineers AND agents -- acceptance tests)

**Environment:** Python 3.11+, data plane stdlib-only; GPU workers pinned per Section 2.
**Ports and windows:** router `:9000`; router-local telemetry `udp/5006`; plane telemetry `udp/5106`; DNS `udp/5353` (POC); whale `:8080`. **Node gate 3s, pool eviction 10s, circuit breaker 5s.** vLLM `:8000`.

| # | Drill | Action | Expected (pass) |
|---|---|---|---|
| 1 | Baseline | requests through the pool router | 200s; Served-By = a live worker; `/__stats` eligible = all workers |
| 2 | Intra-pool failover | kill one worker mid-traffic | Served-By flips to survivor within 3s; **zero client errors**; failovers++, dead node dropped |
| 3 | Mid-stream fail-fast | kill serving worker AFTER first token | clean chunked truncation, no fake resume; midstream_failfast++ |
| 4 | Cross-cloud evacuation | kill an entire pool | `dig` drops that pool's IP within 10s; the other cloud remains |
| 5 | Whale finale | kill all pools | `dig` answers the whale IP; whale returns a graceful degraded 200 (or 503+Retry-After) |

**The landmines (verbatim -- they cost hours):**
- RunPod / CUDA-12.4-era drivers require pinning vLLM 0.11.0 + torch 2.8.0+cu128 + transformers<5 + hf_transfer; a default `pip install vllm` pulls a CUDA-13 build that won't run.
- GCE stop/start can change internal IPs -- reserve static addresses for anything referenced by config.
- vLLM does not auto-start on boot from a manual launch. GCP capacity reservations bill at full rate while idle.
- EngineCore orphan cleanup before a vLLM restart: `nvidia-smi --query-compute-apps=pid --format=csv,noheader | xargs -r kill -9`.
- Fake-worker port 8001 collides with RunPod's pod nginx -- use 8011+.
- Lambda has no stop state -- billed per-minute until `terminated`; terminate destroys the local disk (commit/push before teardown).
- Stopped Linodes still bill the full plan rate -- delete, don't stop, for throwaway nodes.
- LISH/WebLish is an out-of-band serial console that always asks for the root password and ignores SSH -- a recovery door, not a working shell.
- Cloud Shell blocks outbound non-DNS UDP and disconnects -- never run a workload in it; use it only as a control surface.
- A custom-named SSH deploy key isn't offered by default -- add a `~/.ssh/config` IdentityFile entry (check this first when "the key is on the repo but still permission denied").

## 10. What this doesn't do (limitations, stated plainly)

- **No mid-stream resume** -- by design; the honest contract truncates rather than fabricates.
- **Scoring weights are hand-set**, not learned.
- **Cross-cloud evacuation is proven at the DNS/telemetry layer**, not (in the final multi-cloud run) via a live completion handoff onto the survivor -- that leg was blocked by a provider's default inbound firewall on the router port, a config detail rather than a design property (see WRITEUP Section 9).
- **Production hardening is deferred and tracked as issues:** anycast whale, CoreDNS-native frame parsing, frame v2 (HMAC, epoch/boot-id), and control-plane HA (dual planes + a witness for split-brain -- see `docs/diagrams/d5_splitbrain.svg`).

## Appendix: the reading list
- `docs/WHITEPAPER.md` -- the full technical writeup: the narrative and reasoning.
- `docs/diagrams/` -- six architecture views.
- `docs/LESSONS.md` and `docs/NOMAD_NOTES.md` -- every "reality taught us what the tests didn't."
- `docs/evidence/` and `docs/RP_RESULTS.md` -- real logs from real drills.
