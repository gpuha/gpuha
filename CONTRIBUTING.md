# Contributing to GPU HA

Thanks for your interest. This is a reference implementation and prior art for
two-tier HA/DR of LLM inference. Contributions that improve the reference, the
drills, or the docs are welcome.

## Ways to contribute
- **Issues** -- bugs, unclear docs, or a failure mode the drills don't cover.
- **Provider adapters** -- new cloud adapters (the encoded lifecycle scar-tissue is the point).
- **Hardening** -- the deferred production items (anycast whale, CoreDNS-native frame
  parsing, frame v2 with HMAC/epoch, control-plane HA) are tracked as issues; good entry points.
- **Drills** -- more acceptance tests for the failure ladder.

## Ground rules
- Data-plane components stay **stdlib-only** (zero cloud-provider deps) -- keep the failover logic legible.
- Every behavioral claim ships with a runnable drill or captured evidence.
- Be honest about what's stubbed or deferred; name it.

## How
Fork, branch, PR against the default branch. Keep changes focused; for anything
large, open an issue first so we can align. Apache-2.0 applies to all contributions.
