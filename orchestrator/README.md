# gpuha orchestrator (Phase O) — CLOSED source

The product's core loop: acquire GPU/stub pools across providers best-effort,
wire them into the plane's Tier-1 DNS, verify the fabric, and tear everything to
**true zero idle cost**. Per `docs/OPEN_CORE.md` this subtree is CLOSED and must
never be mixed into a public/open subtree.

## Design: teardown-first
- **Persist before create.** `RunState` is written to `~/gpuha-runs/<run-id>.json`
  before the first resource exists and after every create; a crash mid-`up`
  still leaves a teardownable record.
- **`down` is idempotent** and works from persisted state even if `up` half-failed.
  Any unrecoverable error during `up` tears down the partial fabric before raising.
- **`reap` is the backstop.** It queries each provider for anything labelled
  `gpuha-managed=<run-id>` and destroys it regardless of local state.
- Every created resource is labelled `gpuha-managed=<run-id>` at creation.
- Each adapter knows its **billing-correct** teardown verb: GCP delete, Lambda
  terminate, RunPod delete, Linode delete-not-stop. The plane is never touched.

## CLI
```
./gpuha up orchestrator/topologies/demo-minimal.yaml
./gpuha status
./gpuha down [run-id] [--topology <path>]
./gpuha reap [run-id | --all] [--providers gcp_stub,fake]
```

## Layout
- `state.py` — run-state persistence (atomic JSON).
- `topology.py` — stdlib YAML-subset loader + schema.
- `engine.py` — acquire-with-fallback / quorum / teardown / reap state machine.
- `wiring.py` — writes tier1 `--pool-file` and restarts the service.
- `adapters/` — `base`, `fake` (offline proof), `gcp_stub` (M1); Lambda GPU is M2.

## Status
- **M1 (this phase):** the loop proven with a GCP e2-micro **stub** pool (no GPU,
  no inference). Offline loop proven with the fake adapter (`tests/test_loop.py`).
- **M2:** first real Lambda L4 GPU pool (vLLM+router+shim), real streamed completion.
- Multi-pool cross-cloud on-demand is **Phase C′**.
