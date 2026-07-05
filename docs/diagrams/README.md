# Diagrams

Rendered SVG diagrams for GPU HA. These are the **presentation layer** only.
The ASCII diagrams in the architecture write-up remain the diffable
source-of-truth; regenerate/refresh these SVGs when the ASCII changes.

| File | Diagram |
|------|---------|
| `d1_architecture.svg` | System architecture: Tier-1 DNS + Tier-2 L7 router over the shared content-free telemetry contract. |
| `d2_demo.svg` | Failover demo: per-request routing and Served-By flip on worker loss. |
| `d3_harness.svg` | Client-traffic harness (Phase H): stream-safe drain / traffic-management measurement. |
| `d4_orchestrator.svg` | Orchestrator lifecycle: teardown-first up/verify/down/reap, persist-before-create, label-based reap. |
| `d5_splitbrain.svg` | Split-brain resolution: silence=death liveness + quorum/eviction across pools. |
| `d6_opencore.svg` | Open-core boundary: open portfolio surface vs. the CLOSED orchestrator/control plane. |
