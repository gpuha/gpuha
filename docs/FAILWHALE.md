# FAILWHALE.md — Graceful Degradation for LLM Traffic

**Lineage:** in the dnshat era, when blue and green were both down, DNS failed over to a static "fail whale" page — the user saw something human instead of a browser error. This is that pattern rebuilt for OpenAI-spec token traffic: when every GPU pool is dark, clients reach a last-resort endpoint that speaks the protocol and degrades gracefully instead of connection-refusing.

## The hole it fills

Tier-1's FAILSAFE currently fails open to the full pool set — i.e., when everything is dark, DNS points clients at **dead router IPs**. Honest at the DNS layer; broken at the client. The whale is what FAILSAFE should answer with.

## Component: `whale.py` (built, contract-tested)

Standalone stdlib server. **Governing principle: the whale is the dumbest thing in the fleet** — no telemetry, no state, no per-request work; every response is a byte buffer precomputed at startup. A last resort must share fate with nothing.

Behaviors (`--mode`):
- **auto** (default): request sniffed for `"stream": true` → streamed SSE graceful completion; else JSON graceful completion. Both are valid 200 OpenAI shapes whose content is the configured "please give me a moment…" message.
- **error**: protocol-correct degradation — 503, spec-shaped error body (`type: service_unavailable`, `code: gpuha_all_pools_down`), `Retry-After` header. Official SDKs retry/backoff on this automatically: the self-healing path for API clients.
- **complete**: force non-streaming JSON completion.

In-band degradation signal: `model: "gpuha-degraded"` on every whale completion (plus `X-GPUHA-Degraded: true` header). Chat frontends render the message; programmatic clients detect degradation from a field they already parse. Mode choice per deployment: **error** is correct for API-consumer audiences; **auto** is kind for chat-app audiences. Default auto.

Capacity: ~4.9k req/s single-process (sandbox-measured); `--workers N` fans out via SO_REUSEPORT. nginx front is the production escalation if ever needed. CORS is permissive (browser chat apps call these endpoints directly).

## Integration point 1 — Tier-1 FAILSAFE (one-flag change to tier1_dns.py)

Add `--failsafe-ip <WHALE_IP>`. When zero pools are fresh: if the flag is set, the FAILSAFE answer is the whale's A record (alone); if unset, current behavior (full set) remains. Log line unchanged (`FAILSAFE`) plus which answer form was served. Whale runs on the always-on Linode plane next to tier1 — same box is acceptable (both are tiny; the plane is the designed survivor).

## Integration point 2 — router-local whale mode (spec for engineer; do NOT fork router.py outside canonical)

The router's `no_capacity` path (currently bare 503 JSON) gains the same graceful behaviors via `--degrade auto|error` with the same precomputed-buffer approach (import from whale.py's builder). Covers the "my pool's workers are gone but the router host is alive" case — which DNS-level whaling cannot see, because the pool's telemetry going dark takes ~10s while requests fail *now*. First-token contract is unaffected: the whale only ever serves requests that would otherwise get no first token at all; committed streams are never whale-resumed.

## The honest residual gap (writeup material)

When all pools die and FAILSAFE flips DNS to the whale, clients holding cached DNS (public-resolver 20–60s floors) still hit dead pool IPs until their cache expires. The router-local whale covers the "router alive, GPUs dead" slice of that window; a fully dead pool host answers nothing until TTL expiry. Same residual dnshat had. Production mitigations (deferred): anycast the whale IP; or whale-at-the-edge co-located with each pool ingress. Do not overclaim the whale closes the cached window — it closes the *post-TTL* experience and the *pool-alive* cases.

## Placement

Phase L deliverable (deploys with the plane). `whale.py` + `whale_test.py` land in canonical now; tier1 flag and router degrade mode are engineer patches post-D.5. Patent-watch: graceful-degradation endpoints are prior-art-heavy; at most a minor adjunct claim (telemetry-driven failsafe → protocol-faithful degraded completions with in-band model-field signaling) — one line in the invention notes, low expectation.
