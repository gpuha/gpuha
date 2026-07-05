"""GPU HA orchestrator (Phase O). Stdlib-first. CLOSED source (see docs/OPEN_CORE.md).

Public entry point is the `gpuha` CLI (orchestrator.cli). The engine acquires
GPU/stub pools across providers best-effort, wires them into the plane's Tier-1
DNS, and tears everything to true zero idle cost. Teardown-first by construction:
state is persisted before any resource is created, `down` is idempotent from
persisted state, and `reap` queries each provider independently as the backstop
against a forgotten bill.
"""
__all__ = ["state", "topology", "engine", "wiring", "adapters"]
