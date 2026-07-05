# OPEN_CORE.md — What Opens, What Stays Closed, and Why

**Status:** decision framework, agreed in principle 2026-07-03 (Scott + architect).
Final dispositions marked HOLD are gated on a patent-attorney consult. Nothing flips
public until the Gates section clears.

---

## 1. The decision in one paragraph

The open/closed line is **data plane vs. control plane** — NOT Tier-1 vs. Tier-2.
We open-source the components a developer installs and runs *themselves* for GPU HA
inside their own cloud (the T2 data plane: router, selector, shim, whale, frame
schema). We keep closed the components a customer *pays us to operate*: the
multi-cloud orchestrator, provider adapters, cross-cloud evacuation coordination, and
the DR-evidence/compliance engine. Ideas and architecture diagrams are published
freely (blog, talks, writeup); orchestration code is not. This gives Scott the
public-repo/resume/credibility win and preserves the sellable moat, without the trap
of the original T1-closed/T2-open framing — which would have closed the prior-art-heavy
part while giving away nothing protectable either.

## 2. Why not T1-vs-T2

- T1 alone (DNS failover) is 20+ years of prior art — Scott built it himself once.
- T2 alone (weighted LB with health checks) is equally well-trodden.
- The candidate-novel element sits in the SEAM: one versioned, content-free telemetry
  frame driving both per-request L7 selection and DNS pool evacuation, with liveness
  judged by consumer-side arrival freshness (the shared-ingest artifact).
- Therefore the protective line can't follow the tier boundary; it has to follow the
  buy-vs-run boundary. What people run themselves: open. What people pay us to run
  across clouds: closed. The novel seam is handled explicitly (see HOLD items).

## 3. Component disposition

| Component | Disposition | Rationale |
|---|---|---|
| `selection.py` (health gate, scoring, breaker, anti-herding) | **OPEN** | Data plane; useful standalone; prior-art-adjacent; resume gold |
| `router.py` (first-token contract, dual framing, degrade mode) | **OPEN** | The flagship open artifact — "install this in front of your vLLM fleet" |
| `vllm_telemetry_shim.py` | **OPEN** | Required for the open router to function; scrapes stock vLLM |
| `fake_worker.py`, test suites, drill harnesses | **OPEN** | Credibility: shipped with proofs |
| `whale.py` + FAILWHALE design | **OPEN** | Broad goodwill feature; prior-art-heavy (graceful degradation); great demo |
| `telemetry.py` — frame **schema** (v1) | **OPEN** | The open components can't interoperate without it; schema-as-spec |
| `telemetry.py` — shared `TelemetryIngest` used by BOTH tiers | **HOLD** | Opening it *alongside tier1* discloses the cross-tier mechanism — the patent-watch item. Attorney call first |
| `tier1_dns.py` | **HOLD** | Simple code, but it's the disclosure trigger for the novel seam when published next to the shared ingest. Attorney call first |
| `legacy/` (gpuha.go, build.sh, aggregator) | **HOLD** | Publishing Scott's own unpublished prior design would CREATE prior art against a potential patent on the evolved mechanism. Keep private until the patent question is settled; great blog material *described*, not *published* |
| Orchestrator (`gpuha up/down`, topology spec engine) | **CLOSED** | The product's core loop |
| Provider adapters (GCP/AWS/RunPod/Lambda/Linode lifecycle + capacity scar-tissue) | **CLOSED** | The moat is encoded operational knowledge |
| Cross-cloud evacuation coordination / control-plane SaaS | **CLOSED** | What ICP-2 pays for |
| DR-evidence / compliance drill engine | **CLOSED** | The enterprise wedge (MARKET_NOTES) |
| `WRITEUP` (technical) | **OPEN eventually** | §8 (mechanism detail) redacted until HOLD resolves |
| `TARGET_STATE.md`, `MARKET_NOTES.md`, briefs, NOMAD_NOTES | **CLOSED** | Business strategy, ICP research, internal ops — never public |
| Architecture diagrams, two-tier thesis, first-token contract, drill methodology | **OPEN (as ideas)** | Blog/talks/README — concepts build reputation; withholding concepts protects nothing |

## 4. Two-repo structure — NEVER flip the private repo public

The current `gpuha/gpuha` private repo contains briefs with IPs, business docs, market
research, and legacy source. It stays private **forever**. When we go public, we
create a second, curated repo (working name: `gpuha/gpuha-router` or similar) that
receives ONLY the OPEN components plus user-facing docs written fresh for that
audience. Public release is an act of curation, not a visibility toggle. This removes
the single most likely catastrophic mistake (one click exposing MARKET_NOTES and the
orchestrator).

## 5. License recommendation

**Apache-2.0** for the public repo. Rationale: enterprise-friendly (ICP-2 legal teams
wave it through), resume-standard, includes an explicit patent grant *for the code it
covers* — which is fine for the OPEN set precisely because we've excluded the
mechanism we might patent. Interaction to respect: if a HOLD item (tier1 + shared
ingest) later ships under Apache-2.0, its patent grant applies to that code — one more
reason the attorney consult precedes any HOLD→OPEN move. AGPL considered and rejected:
it would deter exactly the enterprise self-hosters we want adopting the open layer,
and our defense against cloud-vendor productization is the closed control plane, not
copyleft. (Also, lightly: "gpuha" as a mark is unexamined — trademark check belongs in
the same attorney conversation. Not legal advice.)

## 6. Gates (in order — none are optional)

1. **Patent-attorney consult** on the cross-tier telemetry mechanism → resolves every
   HOLD (file, or deliberately dedicate to public, or keep trade-secret-closed).
2. **Employment/conflict/IP review** before ANY paid offering (closed control plane as
   SaaS or services). Open-sourcing the OPEN set is inside the safe portfolio lane;
   revenue is not, until this clears. (Standing constraint per STATUS.md.)
3. **Curation pass**: build the public repo from the OPEN list only; fresh README;
   redacted writeup; employer-optics pass on anything narrative.
4. **Then** publish, blog, LinkedIn — in that order (repo first so the post has a link).

## 7. What Scott gets from this shape

Public: a genuinely useful standalone project ("HA for your vLLM fleet across zones,
with an honest first-token failover contract and a fail-whale") + the thought-leadership
layer (thesis, diagrams, war stories) → open-source cred + resume artifact.
Private: the orchestrator, the adapters, the compliance engine, the seam — the things
either ICP would actually pay for. Best of both, with the two decisions that can't be
un-made (publishing the mechanism; taking revenue) explicitly gated.
