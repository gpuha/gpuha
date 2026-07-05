# LESSONS.md — Building GPU HA with Linode + an Agentic Workflow

Running notes. Feeds two outputs: the technical writeup (engineering record) and a
possible build-story blog/LinkedIn post (narrative). Tagged [TECH] / [BLOG] / [BOTH].
Captured live during the build so the detail is real, not reconstructed.

---

## Working with Linode / Akamai

**[BOTH] Stopped Linodes still bill.** Unlike AWS (stop = EBS only) or GCP (stop =
disk only), a stopped Linode reserves the plan and bills the full monthly rate. To
stop paying you delete (or snapshot + delete). This shaped the whole target-state
cost model: the plane is *deliberately* always-on because "stopped and cheap" isn't a
Linode state; everything ephemeral gets deleted, not stopped.

**[BOTH] Old instances are silent monthly drains.** Five boxes from prior R&D were
sitting stopped-but-billing at ~$68/mo — a `coredns-edge` 8GB ($48) plus four Nanodes.
"Stopped" felt free; it wasn't. Lesson: inventory and delete aggressively; a stopped
box you're not using is pure waste on this provider.

**[TECH] LISH/Weblish is out-of-band and password-gated by design.** The serial
console emulation ignores SSH entirely and always prompts for the root password —
it's a physical-console stand-in, not a login shell. Trying to drive it for routine
command work is the wrong tool: no key auth, and terminal output must be
screenshot-read. Use it only as the true last-resort recovery door (network down, SSH
locked out). For everything else, key-based SSH or a web shell (JupyterLab).

**[TECH] ufw hardening looks identical to a dead box from the outside.** `coredns-edge`
appeared "unreachable" in an earlier phase (SSH + port 53 filtered) and was assumed
dead/old. It was actually recent (created days prior) and simply hardened per its own
build script: default-deny inbound, port 53 open only to five anycast /24s, 5006 open
only to the aggregator IP. Lesson: "unreachable from where I happen to be probing" ≠
"dead." Check the firewall rules before writing off a box.

**[BLOG] The domain's live A record was pointing at a ghost.** The recovered zone file
showed `api.gpuha.com` still resolving to a Lambda IP that no longer existed — the old
hidden master's *last computed routing decision*, frozen in DNS, outliving the
infrastructure it pointed at. A small, almost poetic reminder that DNS state is sticky
and survives the things it describes (which is, after all, half the project's thesis).

**[BOTH] Keep the DNS Manager zone even when nuking compute.** Deleting the instances
is fine; deleting the hosted `gpuha.com` zone would strand the domain. Free-tier,
separate from compute — explicitly preserved through the cleanup.

## Working with an agentic engineering workflow (supervisor + architect + engineer)

**[BOTH] The three-role split worked.** Human (Scott) as supervisor/decision-maker, a
reasoning model as architect (design, briefs, code artifacts, review), and a
browser-capable agent (Cowork) as engineer (drives real consoles/UIs, runs commands,
executes briefs). Clear division: the architect never touches infra, the engineer
never redesigns, the human approves anything irreversible. Briefs are the interface.

**[TECH] Match the tool to the door.** The recurring failure mode: a browser-driving
agent reaches for browser-shaped solutions (serial console, clicking dialogs,
screenshot-reading terminals) even when a proper shell exists. Driving a Linux box
command-by-command through a screenshot-read serial console is slow and error-prone
(stale DOM, gapped captures, reordered scrollback). The fix is to get a *real* shell
fast — SSH for a human, or stand up a web shell (JupyterLab) the agent can reach — and
abandon the console the moment one exists.

**[TECH] The bootstrapping gap is inherently the human's to cross.** A fresh Linux box
has no door a browser agent can use except the password console — until a web shell is
running. Crossing that first gap needs the SSH private key, which lives with the human
and should never touch the agent's sandbox. So "spin up and configure a brand-new box
fully autonomously" isn't achievable for a browser agent; the human does the minimal
key-gated bootstrap (SSH in, run one script to bring up JupyterLab), then hands off.
Design the workflow around this rather than fighting it.

**[BOTH] Never hand a task to a browser shell when you already hold a real one.** Late
in the build, time was lost trying to route a git push through the agent's web-shell
path while the human was *already SSH'd into the box with a real terminal*. If you're
standing in the shell, do the shell thing; hand the agent the browser-gated pieces
(creating a private repo, adding a deploy key) where it's genuinely strong.

**[TECH] Agents should verify capability, not claim it.** A good moment: told to "SSH
in with the key," the engineer *tested* it, found its sandbox had no egress and never
held the private key, and said so plainly instead of pretending. Contrast with the
earlier architect instruction that assumed a capability that didn't exist. Lesson for
both sides: probe the actual toolset before asserting a path; a concrete "I can't
reach X, here's the error" saves more time than a confident wrong plan.

**[BOTH] Credentials never pass through the agent.** SSH private keys generated by the
human in their own terminal; root passwords typed by the human into the console, never
by the agent; deploy keys generated *on the box* with only the public half shown to
the agent. The agent handles the browser clicks that need a logged-in session; the
secrets stay with the human and the box. This held throughout and is the right default.

**[BOTH] Cross-AI-stream recovery: copy from the source, don't exfil from the deploy.**
An hour was nearly lost trying to claw legacy source files off a deployed box via
console, when the same files were sittable in the *original* AI chat (a different
model's session) they'd been authored in — a scroll-and-paste away. Lesson: when work
spans multiple AI tools/sessions, the fastest recovery of an artifact is usually the
chat that created it, not the machine it was deployed to.

**[BLOG] The infrastructure kept proving the thesis while we built on it.** GPU HA's
premise is "GPU capacity is volatile and you must fail across providers." During the
build we were capacity-blocked on GCP (quota auto-denied, zones stocked out), then hit
RunPod marketplace exhaustion mid-project (couldn't restart the very pod we'd used
days earlier), then migrated to Lambda — three providers in one week, each failing in
a different way. The project's own premise was demonstrated *by the environment* while
we were trying to build the thing that solves it. That's the narrative spine of the
blog post.

## Meta

**[BLOG] "Done beats perfect" at 2am.** Several stalls came from chasing the elegant
path (key-based SSH, scp-not-paste) when the working path (reopen the console, type
the password, move) was right there. Worth naming: when tired, the correct call is
often the un-clever one that unblocks now.

## [BOTH] Test/utility nodes default to GCP cheap CPU; workloads run ON the box
Standing rule (Phase L). Throwaway test/utility nodes default to **GCP e2-micro/e2-small**,
agent-driven end-to-end via Cloud Shell `gcloud` (create / configure / delete). Critical
caveat: **Cloud Shell is a CONTROL surface only — never run the workload in Cloud Shell.**
It blocks non-DNS outbound UDP and drops on idle/large pastes; this broke the first D.5
cross-internet transport attempt (`socket.sendto()` returns success locally, packets never
egress). SSH to the instance and run the emitter/load **on the instance**, which has clean
UDP egress. Permanent plane + demo/GPU pools stay on **Linode / best-effort multi-cloud**.

### SSH gotcha: custom-named deploy key needs an ~/.ssh/config IdentityFile entry
Symptom: `git@github.com: Permission denied (publickey)` even though the deploy key is
registered on the repo. Cause: a non-default key filename (e.g. `~/.ssh/gpuha_deploy`) is
not auto-offered by ssh. Fix — add to `~/.ssh/config`:
```
Host github.com
  IdentityFile ~/.ssh/gpuha_deploy
  IdentitiesOnly yes
```
Check `~/.ssh/config` FIRST whenever "the key is on the repo but it's still denied."

## [PHASE O / M1] On-demand capacity: fabric-verify must POLL, not one-shot
Verifying a freshly-acquired cloud pool must poll until it warms, not check once.
A real instance needs time before it emits: ~45-90s for a GCP e2-micro stub (boot
+ curl emitter from the plane + systemd start), and minutes for real vLLM. A
one-shot dig fires while the pool is still dark, sees FAILSAFE, and under
teardown-first tears the brand-new instance right back down. The orchestrator's
verify now polls dig on an interval with a generous deadline (210s for stubs;
budget 300s+ for GPU pools). Caught only against real infra, not in the offline suite.

## [PATTERN] "Behaves differently against the real target than the mock" — 2nd occurrence
The tier1 `--pool-file` patcher's regex matched the two-line `--failsafe-ip` block
differently on the real file than on the hand-built mock, so the new argument
silently didn't register (argparse "unrecognized argument") even though
`py_compile` passed. Fix: anchor idempotent patches on unambiguous strings (e.g.
`parse_args()`), and ALWAYS verify by RUNNING the patched program, not just
compiling it. This is the SAME bug class as §7.2 (framing dispatch: the router
assumed chunked encoding because the test doubles always streamed; real vLLM
buffered responses broke it). The pattern: mocks inherit your assumptions, so the
real dependency is the test. Budget a "first contact with the real target"
debugging pass for every component, and never let a mock be the last thing a
change is validated against.

## [SIGNATURE FINDING] "The real dependency is never the mock" — 8 occurrences and counting
This is the project's most repeated lesson: every component that passed against a
mock/test-double/local-stub/reasoning-model-in-my-head broke on first contact with
the real dependency — in a way the mock could not have surfaced. Budget a dedicated
"first contact with the real target" debugging pass for every component, and never
let a mock be the last thing a change is validated against.

Occurrences to date:
1. **§7.2 framing dispatch** — router assumed chunked transfer-encoding because the
   test doubles always streamed; real vLLM buffers non-stream responses (Content-Length)
   -> false breaker trips against a healthy worker.
2. **M1 tier1 `--pool-file` patcher** — regex matched the two-line `--failsafe-ip` block
   differently on the real file than on the hand-built mock; the new arg silently didn't
   register even though `py_compile` passed.
3. **M1 on-demand verify** — a one-shot dig against a mock returns instantly; a real
   instance needs 45-90s to boot+emit, so the check fired into FAILSAFE and (teardown-first)
   tore the healthy instance down. Fix: poll.
4. **M2 `fake_worker.py` missing** — `router.py` imports `fake_worker`; I fetched `whale.py`
   for the bootstrap but missed this one -> router crashed on import, HTTP=000, while
   vLLM+shim ran fine and the pool showed "live" in DNS.
5. **M2 nohup-over-SSH** — `nohup ... &` returns cleanly in reasoning; over a real SSH
   channel it never closes, so the kickoff timed out and teardown-first killed a healthy
   booting instance. Fix: `setsid` + tolerate the channel-close timeout.
6. **M2 Lambda API reality** — the edge 403s the default `python-urllib` User-Agent (use
   curl); `terminate` returns HTTP 500 while an instance is booting/terminating (retry).
7. **C'-a `gpu_workers` plumbing** — my string-replace to thread `gpu_workers` into the
   ResourceHandle didn't match the real code (a stray paren), so bootstrap silently fell
   back to 1 worker. Only caught by grepping the actual file, not the patch's success msg.
8. **C'-a two-vLLM-on-one-A10** — "0.42 util is plenty" in my head; the real GPU said
   "No available memory for the cache blocks" (KV), then "CUDA OOM warming up the sampler
   with 256 dummy requests." Fix required staggered start + util 0.38 + max-model-len 2048
   + max-num-seqs 8, all discoverable only against the real card.

The corollary rule that most improved outcomes: **verify by RUNNING against the real
thing, not by compiling / asserting the patch applied / reasoning it through.**

## [SIGNATURE FINDING] Occurrence #9 — the mock was the fetch LIST
RunPod bootstrap fetched router.py + vllm_telemetry_shim.py + fake_worker.py. On the real pod both crashed at import: router needs `selection` (Worker/WorkerSelector/SelectorConfig) and `whale` (build_responses); shim needs `telemetry` (TelemetryFrame/TelemetryIngest). vLLM was healthy the whole time, so the router just silently wasn't listening -> `router_serving` timed out and the completion returned nothing (a silent partial, not an error). Fix: fetch the FULL local-module set (router, vllm_telemetry_shim, fake_worker, selection, telemetry, whale). Reinforced corollary: a bootstrap is only proven when a completion actually STREAMS through the router, never when the process was merely kicked.

## Crash-during-create orphan: state-based cleanup is not enough
#3a kill test: SIGKILL mid gcloud-create -> run-state empty (resources=[]), but 2 instances got created = orphans. Only the label-based `reap --all` caught them. Adapters create-then-persist, so the label/name-prefix reap is LOAD-BEARING, not optional. The real crash window is between provider-create and state-persist -- invisible to state (a cousin of "the real dependency is never the mock").

## Host-level (router-head) failover proven — and the eviction-window stall
RH-P2 live (2x Lambda A10): A = router-head + worker A (vLLM 127.0.0.1); B = worker B (vLLM 0.0.0.0:8000 but iptables-scoped to A). Router on A fronts w1(local)+w2(B via same-region private IP). Both served real tokens (16 w1 / 8 w2 / 24, 0 err). NEGATIVE firewall test PASSED: plane->B:8000 = connection failed (DROP working); A->B:8000 = 200 -> B's :8000 unreachable from any non-A host (no unauthenticated GPU). Kill = REAL INSTANCE TERMINATION of B mid-traffic (200-req loop): Served-By flipped w2->w1, router EVICTED w2 (eligible [w1,w2]->[w1]), all 154 post-kill reqs served by w1 on the OTHER host = host-level failover, closing the C'-a co-residency caveat.
HONEST FINDING (not zero-error): exactly 1/200 requests stalled to the 8s client timeout at the kill instant. Router /__stats: requests=225 ok=224 failovers=0 midstream_failfast=0 -- the 1 failure incremented NEITHER counter. Cause: the router's pre-token retry fires on CONNECTION errors (refused/reset), but a TERMINATING host briefly ACCEPTS-then-HANGS; the in-flight request rode the client timeout instead of being retried, and silence-eviction (3s window) hadn't fired for it yet. Failover is connection-error-driven, not timeout-driven; grey-failure (accept+hang) during the eviction window is a real, narrow gap (<=1 req per host-loss).
Cold-load baseline (A10, Qwen2.5-3B; for next session's in-pod-parallelism compare): vllm serve -> serving-ready = ~63s (weights load 10.9s + init-engine/KV/CUDA-graph warmup 35.7s). The long pole was Lambda instance provisioning (2nd instance booting took several min).

## Meta-pattern (WHITEPAPER §7): "two things you assumed were identical are different"
Three separate bugs have now had the same shape: two conditions we had collapsed into one turned out to be distinct, and the safety logic only covered one of them. Each fix widened the definition of "dead" to include a failure mode we had implicitly excluded.

1. VRAM-zombie -- *process liveness != resource liveness.* The vLLM process was alive and answering health while the GPU/KV state was dead. "Is the process up?" and "can it actually serve?" are different questions.
2. Kill-test orphan -- *state truth != provider truth.* Persisted run-state said "no resources" while the provider had live, billing instances (crash between provider-create and state-persist). "What our state records" and "what the cloud actually bills" are different; the label/name-prefix reap is load-bearing precisely because of this gap.
3. Grey-failure (RH-P2) -- *"worker refused" != "path to worker went silent-but-not-refused."* The router's honest first-token contract retried on connection errors (refused/reset) but NOT on a terminating host that accepts-then-hangs. A refused connection and a hung connection are different failures; honest pre-token recovery must handle BOTH. This is the first-token contract meeting a failure it hadn't fully covered.

The design rule that falls out: liveness/failure logic must enumerate the failure MODES, not just negate the happy path. "Not responding" is at least {refused, reset, hang/timeout, wrong-answer}; "dead" is at least {process gone, resource gone, provider gone, state gone}. The contract is only as honest as the set of failures it recognizes -- every one of these bugs was a failure the code silently assumed couldn't happen.

## GF-P2 live: grey-failure fix PROVEN on real hardware (2x Lambda A10)
Re-ran the exact RH-P2 drill with the fix (router.py PRE_TOKEN_DEADLINE=4s). Same setup: A = router-head + w1, B = w2 on a SEPARATE instance; 260-req loop (curl -m8) through A's router; TERMINATE B's instance mid-traffic (kill 00:09:31Z). Result:
- ZERO client errors: all 260 loop requests HTTP 200 (non-200 count = 0). The request that hit dying B shows a ~4s gap (00:09:40 -> 00:09:44 = the PRE_TOKEN_DEADLINE) then returns 200 -> it STALLED then RETRIED to w1 and succeeded, instead of riding the 8s client timeout (RH-P2 lost exactly that 1 request).
- failovers=1 in router /__stats (was 0 in RH-P2 for the identical stall) -> the stall is now correctly COUNTED as a failover.
- Served-By flipped w2 -> w1; w2 evicted (eligible=[w1]); all post-kill served by w1 on the surviving host.
The first-token contract now covers BOTH connection-refused AND accept-then-hang (the "two failures you assumed were one" from the WHITEPAPER-7 meta-pattern). RH-P2 vs GF-P2: 1/200 lost -> 0/260 lost; failovers 0 -> 1. Teardown $0.

## Lesson - measure the bottleneck before parallelizing it (IPP-P2b, 2026-07-05)

The in-pod-parallelism task assumed the model download was a meaningful slice of cold start worth
overlapping with pip. Clean measurement said otherwise: download = 8.0s, pip = 128.4s. Parallelizing
the download saves at most ~8s (~4%) - the optimization was aimed at the wrong cost. The GPUHA_TS
phase instrumentation is what turned the assumption into a number, and the number redirected the
work: the lever is the pip (uv / pre-baked image), not the download.

Corollary: the automated pre-download silently no-op'd TWICE (huggingface-cli printing help), which a
less-instrumented run would have scored as a parallelism "win". Trust the phase timestamps, not the
absence of an error. Same family as the VRAM-zombie and grey-failure catches: the thing you assumed
was happening (download running / worker failing over) was not - only measurement/instrumentation
exposed the gap.
