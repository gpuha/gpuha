# NOMAD_NOTES — provider-hopping friction log (product research)
The ICP for GPU HA is the developer stitching together whatever GPU capacity exists
anywhere. Scott's own week of provider-hopping IS that customer discovery. Timestamped
friction, backfilled from session history.

## GCP (Phase A, 2026-07-03)
- Signup: pre-existing (personal acct, personal billing).
- First inference: BLOCKED. L4 stocked out across ALL us-east1 zones (b/c/d); L4 quota = 1,
  regional only. Needed a custom `gpu_zone_sweep.sh` to even discover capacity — stockout is
  invisible until you try to start an instance and eat a ZONE_RESOURCE_POOL_EXHAUSTED error.
- Friction: capacity discovery is DIY; quota-of-1; "reserve capacity idle = full bill" trap.

## RunPod (Phase A, 2026-07-03)
- Signup: card + prepaid credits ($20). No pay-as-you-go; must pre-load.
- First inference: ~15-20 min. Deploy L4 pod easy (~$0.40/hr), but vLLM install was CUDA-pin
  hell: pod driver 550/CUDA-12.4 vs latest vLLM pulling a CUDA-13 torch -> "driver too old
  (12040)". Fix needed an exact pin (vllm 0.11.0 + torch 2.8.0+cu128 + transformers<5 + hf_transfer).
- Web terminal usable but reconnects FREEZE the display (toggle tabs to refresh).
- On RESTART (Phase D): pod would NOT start ("not enough free GPUs on host"); CPU-only toggle
  in the Start dialog is non-functional. A stopped pod is NOT a guaranteed-resumable pod.
- Cross-cloud file transfer: runpodctl relay ports blocked from GCP Cloud Shell egress.

## Lambda (Phase D.5, 2026-07-03)
- Signup: pre-done, card verified, PAY-AS-YOU-GO (no credits to pre-buy — nicer than RunPod).
- Dashboard: clean; INSTANCES/FIREWALL/SSH KEYS/USAGE; drive path = SSH **or JupyterLab**.
- BILLING LANDMINE: NO stop state. Bills per-minute until TERMINATED. Terminate wipes local disk.
  Persistent filesystem bills per-GiB-month. ($583 forgotten-instance lore.) Zero egress fees.
- [FILL DURING RUN: launch capacity by region, signup->first-inference wall-clock, firewall UX]

## Cross-cutting
- Every provider = its own environment file (STATUS.md / RUNPOD.md / LAMBDA.md) and its own
  CUDA-pin discovery. Capacity is NEVER guaranteed (GCP stockout, RunPod host-full, Lambda
  region stockout expected). Moving code BETWEEN providers is the recurring tax — this project
  has used GCS signed URLs, Jupyter-proxy download, and git bundles, none universal.

## GCP e2-micro (Phase O M1 throwaway pool)
- Boot + startup-script (curl emitter from plane + systemd start) to first
  telemetry frame: ~45-90s. Orchestrator verify must poll (see LESSONS).
- Spend is effectively $0 (~$0.0084/hr); a full up/down/reap cycle costs a
  fraction of a cent. Good default for throwaway/test pools.
- `gcloud delete` is synchronous; `gpuha down` + `reap --all` confirmed clean via
  `gcloud compute instances list --filter labels.gpuha-managed:*` (empty).

## Lambda (Phase O M2 — expectations)
- Lambda stocks out by region/type; `acquire` should try regions in order and log
  each capacity-out here. TERMINATE-only billing trap: no stop state; bills until
  terminated; terminate destroys the local disk (commit/push before teardown).

## RunPod adapter (full-run session) — real findings
- **L4 gpuTypeId is `NVIDIA L4` (24GB), SECURE cloud only** (community=False). Pod created in EUR-IS-2 (Iceland), costPerHr $0.39.
- **Networking is direct-TCP, not NAT-proxy here**: `GET /v1/pods/{id}` returns `publicIp` + `portMappings` (e.g. `{"22":30144,"8011":30145}`) ~30s AFTER `desiredStatus:RUNNING` (poll for it). SSH works at `ssh -p <mapped22> root@<publicIp>` with the plane key injected via `env.PUBLIC_KEY`. On this DC the pod egress (`ifconfig.me`) == publicIp, so the anticipated NAT gap did NOT manifest — but the adapter detects egress dynamically for DCs where it would. **DNS A record advertises publicIp only; the router's real endpoint is publicIp:<mapped8011>** (port caveat — DNS carries no port).
- **CUDA pin held**: host driver was 565.57.01 / CUDA 12.8 (newer than RUNPOD.md's 550/12.4). `allowedCudaVersions:[12.4..12.8]` + `torch==2.8.0+cu128` is a correct match. `import torch;torch.cuda.is_available()` path is fine.
- **BIG FINDING — cold `pip install torch+cu128 + vllm(0.11)` on a fresh L4 pod takes >20 min** (multi-GB torch/xformers/nvidia-cuda wheels + resolver). This blew past a 15-min verify budget in the solo smoke; vLLM never got to start. **Implication for multi-pod runs: cold-install-per-pod does NOT scale** (5 pods x ~20min serial is untenable, and even parallel it's fragile). **Fix before P3/P4: bake a custom RunPod image (or template) with the pinned torch/vllm/transformers/hf_transfer pre-installed**, so bootstrap only fetches gpuha code + starts vLLM+router+shim (~2-3 min). This is the RunPod analogue of Lambda's faster cold start.
- Adapter reap: `reap(None)` lists `/v1/pods`, filters name-prefix `gpuha-managed--`, `DELETE /v1/pods/{id}`, confirms empty. Proven live (caught + deleted the smoke pod). DELETE, never stop.

## Deadman test-fire — fired in anger, caught a real bug (the whole point)
- Armed a 5-min timer against ONE real billing L4 pod (`gpuha-managed--deadmantest--0`, $0.39/hr) and walked away.
- **RESULT: works.** Deadman fired on schedule, sourced .env, hit RunPod API, matched by name-prefix, DELETED the pod, confirmed `runpod clean`. Post-fire API check: RunPod NONE, Lambda NONE, billing $0. The billing-critical direct-API kill path is proven live.
- **BUG FOUND** (exactly why you test-fire instead of syntax-check): the LAST step — the `./gpuha reap --all` orchestrator backstop — crashed with `AdapterError: unknown provider: gcp`, because "gcp" was added to `KNOWN_PROVIDERS` ahead of building the GCP GPU adapter. It crashed AFTER the direct kills already cleaned up, so this fire was safe — but a backstop that reliably crashes is not a backstop.
- **FIX**: (1) removed premature "gcp" from `KNOWN_PROVIDERS`; (2) made `engine.reap()` resilient — each provider's adapter build+reap is wrapped in try/except so one missing/misconfigured provider can never abort the whole sweep. Re-verified: full `deadman.sh` now runs end-to-end clean (`reap: nothing ... (clean)`, `DEADMAN DONE`, no traceback).
- **LESSONS**: (a) a billing-defense tool is only trusted after it fires in anger; (b) never widen `KNOWN_PROVIDERS` before the adapter exists; (c) the reaper must be defensive against its own misconfiguration — belt-and-suspenders means the suspenders can't depend on the belt being perfect.

## Standing design rule (safety-path code)
**"The suspenders can't depend on the belt."** Any cleanup/safety path (reap, deadman, teardown) MUST tolerate the system being broken, because a broken system is the only time it runs. Concretely: per-provider try/except isolation, no hard dependency on optional components, degrade-and-continue over abort. Applies to all future safety-path code.

## #73 bake — pre-logged constraints (RunPod network-volume venv)
1. **Provider/shape-specific, by design.** A pre-installed venv of compiled CUDA wheels is pinned to Python version + CUDA runtime + GPU family. Lambda/GCP cannot mount a RunPod volume. This fast-start answer is **RunPod-only**. The cross-cloud matrix (P3+) needs its own per-provider fast-start (Lambda: image/pin already fast; GCP: DLVM image or a boot script) — DEFERRED, but noted now so it can't ambush P3. **Action: pin the #73 pod template (GPU type=NVIDIA L4, host image=runpod/pytorch:2.8.0-...cuda12.8, Python 3.11) to match the volume venv exactly.** Mismatched GPU family / CUDA / Python = broken venv.
2. **Region pinning.** A RunPod network volume lives in ONE datacenter and constrains where pods using it can spawn. If a future run hits "no capacity," the volume's DC is the first suspect. **The chosen DC is recorded below when the volume is created.**

- **Volume created: `cj1u06pz8s` name=gpuha-bake, 50GB, DC=EUR-IS-1.** Pods using it are pinned to EUR-IS-1. Storage bills ~$0.05/GB/mo -> DELETE when demo done. Baked venv at /workspace/gpuha-venv, model cache at /workspace/hf.

## Volume-bake + fast-start timing (RunPod, real numbers) — VERDICT: DELETE volume
Baked pinned venv (torch 2.8.0+cu128 + vllm 0.11 + xformers) + Qwen2.5-3B model onto a 50GB RunPod network volume (cj1u06pz8s, EUR-IS-1), then timed a FRESH pod mounting it cold.
- **Bake write:** ~35 min to write venv(16GB)+model(5.8GB) to the volume (mfs); ~0.6 GB/min. Slower than a local-disk cold install because every write is network I/O.
- **Fast-start (fresh pod, cold mount):** create->ready 31s | torch+vllm import off volume +68s | **vLLM cold-load (model read off volume + CUDA graph) +427s (cold_load 359s)** | router serving/first-token +513s. **Total mount->first-token ~544s (~9 min).**
- **Verdict: DELETE the volume (>5-min threshold).** The bake DID beat a cold pip install (~9 min vs ~23 min — skips the 20-min pip), but network-volume READS make vLLM cold-load ~6 min, so it never reaches fast-start. Reading ~22GB of venv+model over mfs is the bottleneck. Volume+pod deleted; RunPod PODS/VOLUMES NONE.
- **Decision (per architect):** POC default = SIMPLE path (cold install + big warmup_budget + parallelize across pools). Docker image + tiered hot/warm/cold readiness = product scope (see TODO_HARDENING).
- **Adapter reap-hang:** adapter reap(None)'s DELETE-then-GET-confirm loop hung/lagged over the terminal during teardown; a direct API DELETE (HTTP 204) cleaned it. TODO: hard timeout on _delete so a safety path can't block (per "suspenders can't depend on the belt").
- **P1 serving proof (CLOSED):** completion through the pool router returned HTTP 200, X-GPUHA-Served-By: rp-w1, real tokens "GPU HA Online" (4 completion_tokens), served by the real GPU-backed vLLM running from the baked venv. Router binds 0.0.0.0:8011 (RunPod direct-TCP proxy reaches it).

## #3a live (2x GCP e2-micro stub, real gcloud) — concurrency + KILL test
- **Auth fix (durable):** the interactive user token expired mid-session. Switched to a SERVICE-ACCOUNT key (gpuha-orchestrator@gpuha-dev.iam; roles Compute Admin + Service Account User), `gcloud auth activate-service-account`. Survives token expiry -> the right answer for unattended runs. Key lives OUTSIDE the git repo (~/gpuha-workbench/gpuha-dev-*.json), chmod 600, gitignored, never printed. deadman/reap use it automatically (persists in gcloud config).
- **Concurrent bring-up: 16.5s wall; serial (GPUHA_SERIAL=1): 34.9s** for 2 real e2-micro pools. ~2.1x; wall-clock = the slowest pool, as designed. Both landed both runs; reap clean each time.
- **KILL TEST (parallel-era deliberate-orphan test):** SIGKILLed the orchestrator at ~11s (mid gcloud-create). Persisted run-state = `status=initializing, resources=[], pools={}` -- EMPTY. Meanwhile the detached gcloud children finished and created BOTH instances (STAGING) = orphans INVISIBLE to state. `reap --all` (label-based, state-INDEPENDENT) found + destroyed both. Final: GCP 0, RunPod 0, Lambda 0 = $0.
- **FINDING (load-bearing):** the label-based reap is NOT a mere backstop -- it is the ONLY thing that catches a crash-during-create orphan, because gcp_stub (and lambda/runpod) create the cloud resource BEFORE persisting the handle, violating "persist before create". State-based teardown alone would have leaked both. Fix -> TODO_HARDENING.

## RH-P2 router-head on Lambda (2x A10) — friction
- Cross-host worked over PRIVATE IPs (both instances same region, 10.19/16): router A -> B(priv):8000 OK; B shim -> A(priv):5006 OK (router telemetry ingest binds 0.0.0.0). No account-firewall surprises on the private path.
- Plane telemetry FIREWALL GATE scoped only A's /32 (handle.public_ip); B's frames to plane:5106 are dropped. Not load-bearing (A keeps the pool alive in DNS; the w1/w2 failover is at A's local :5006), but if per-node plane telemetry from siblings is ever wanted the gate must add sibling IPs.
- 2nd instance (B) boot was the slow part (~several min booting); warmup_budget=1800 (RH-P1) gave ample headroom and was honored per-pool.
- Spend: 2x gpu_1x_a10 for ~1 run; both terminated; final $0 confirmed (0 gpuha instances).

## IPP-P2 in-pod parallelism: live single-A10 run (honest, confounded)
Harness + GPUHA_TS phase markers worked; clean phase breakdown (A10, Qwen2.5-3B):
- script_start -> hf_ready: 6.1s (venv + small huggingface_hub[cli]/hf_transfer pip)
- big torch+vllm+transformers pip: 131.9s (the dominant cost)
- vLLM serve -> ready: 69.2s (weight load 10.2s + engine/KV/CUDA-graph init ~35s + overhead)
- TOTAL bootstrap (script_start -> serve_ready): 207.5s (~3.5 min)
BUT the parallelism did NOT kick in: the background pre-download's dl_done fired 0.26s after dl_start = a no-op. Likely cause: the boot script exports HF_HUB_ENABLE_HF_TRANSFER=1 and the `huggingface-cli download` errored immediately (a manual download WITHOUT that env var downloaded fine). Worse, a mid-run manual diagnostic download pre-cached the 5.8GB model, so `vllm serve` was fast (~69s ~= the pre-cached baseline) for the WRONG reason -> serve time is confounded. Net: no clean parallelism speedup demonstrated this run.
Opportunity still confirmed by the phase data: the model download (~20-40s over HF) fits ENTIRELY under the 132s torch+vllm pip install, so a working background download overlaps it fully -> expected ~20-40s off the ~207s total and serve->ready back to ~45s (load+init only). FIX applied: pre-download now runs with HF_HUB_ENABLE_HF_TRANSFER unset, logs to /home/ubuntu/dl.log, marks GPUHA_DL_FAIL on error (robust + diagnosable). Clean re-run needed to measure the actual savings. Teardown $0.

## IPP-P2b - clean serial cold-start baseline (2026-07-05, single A10, Qwen2.5-3B)

Clean SERIAL run, no cache interference. GPUHA_TS phase markers, script->serve_ready:

| phase | delta | note |
|---|---|---|
| venv + small HF pip | 5.9s | |
| big torch+vllm pip | 128.4s | 65% of total - THE bottleneck |
| serve_start->ready | 63.2s | 8.0s weight dl + 9.3s load + ~35s engine/KV/CUDA-graph |
| **total script->ready** | **197.7s** | |

vllm.log: "Time spent downloading weights ... 7.999527 seconds"; "Model loading took 5.7916 GiB and 9.27s".

KEY: model download is only ~8s (hf_transfer saturates Lambda's net for ~6GB), not the 20-40s
estimated. So in-pod parallelism (hiding the download under the pip) has a CEILING of ~8s ~= 4%
of total. The real cold-start lever is the 128s torch+vllm pip install - attack it with uv or a
pre-baked venv image, NOT download parallelism.

CLI bug: `huggingface-cli download <repo>` printed `hf --help` and no-op'd (0.28s, cache 12K) on
this hf_hub version. Correct programmatic fetch: `python3 -c "from huggingface_hub import
snapshot_download; snapshot_download('Qwen/Qwen2.5-3B-Instruct')"` (keep HF_HUB_ENABLE_HF_TRANSFER=1).
Fixed in BOOTSTRAP_TMPL. Teardown $0.

## RunPod router-head + multi-cloud live run (2026-07-05)

- RunPod cold-to-serving ~3.5min (torch cached in runpod/pytorch image) -- the old ">20min cold" note is stale.
- RunPod SECURE pods share a proxy public IP; workers differ by mapped port. A and B can land on the same or
  different proxy IPs. Direct-TCP proxy is TCP-only (UDP telemetry cannot traverse it) -> we RELOCATE B's shim
  to pod A (A scrapes B over the proxy, emits frames to A's local router). Preserves silence=death.
- RunPod vLLM /metrics is NOT gated by --api-key, so the relocated shim scrapes it without auth (w2 registered).
- LAMBDA FRICTION: Lambda cloud now blocks inbound :9000 by default (router port). SSH :22 and outbound
  telemetry (udp/5106) work, so the pool still REGISTERS in tier1, but the plane cannot reach the router to
  serve completions. Router is healthy locally (LISTEN 0.0.0.0:9000). Fix path: add a Lambda firewall inbound
  rule for :9000 (account-level, likely a dashboard/API action). Classic provider-default drift.
- worker_only pods are adapter-direct only (no router/shim -> not tier1-servable -> `gpuha up` verify self-tears).
