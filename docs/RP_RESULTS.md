# RunPod router-head + multi-cloud C'-c -- LIVE RESULTS (2026-07-05)

Total spend ~$1.2. RunPod cold-to-serving ~3.5min (image has torch cached; the >20min NOMAD note is stale).

## RP-P1 RunPod solo + 401 GATE (adapter-direct): PASS
Simultaneous on B public proxy port: no-key=401, with-key=200. The api-key IS the firewall. Torn down, $0.
Finding: worker_only pods are intentionally NOT tier1-servable (no router/shim), so `gpuha up` engine-verify
(dig) fails and self-tears-down -- the solo GATE must be driven adapter-direct.

## RP-P2 RunPod router-head (2 pods): PASS
eligible=[w1,w2] (w2=remote B via A's RELOCATED shim scraping B /metrics through the proxy; the
/metrics-api-key risk did NOT materialize -- no shim --auth needed). 401 gate on B re-confirmed.
Baseline 15/15 200 (6 w1 + 9 w2). Kill drill (real pod DELETE of B mid-traffic): 88/88 client 200,
ZERO errors; router requests=104 ok=103 failovers=1 midstream_failfast=0; eligible [w1,w2]->[w1];
Served-By flipped w2->w1. Host-level intra-pool failover across a NAT'd worker PROVEN. $0.

## RP-P3 combined multi-cloud (Lambda survivor + RunPod RH): PASS with provider caveat
Bring-up 2/2 pools ok, both in tier1 dig (Lambda 132.145.161.8 + RunPod 213.173.105.13); plane udp/5106
scoped to both egress /32s. RunPod baseline 5 w1 + 5 w2 all 200.
Leg4 CROSS-CLOUD EVACUATION: killed RunPod pool -> T+15s dig dropped RunPod, left ONLY Lambda. PROVEN.
Leg5 WHALE: whale serves degraded (model=gpuha-degraded); after unwire dig returns whale 203.0.113.10. PROVEN.
CAVEAT: Lambda completion path unreachable from plane -- Lambda cloud firewall blocks inbound :9000
(router healthy locally: LISTEN 0.0.0.0:9000, local /v1/models via router 404=alive, vllm 200; SSH+telemetry OK).
Provider-default-firewall change, not a gpuha defect. Cross-cloud legs proven at the tier1/DNS layer (telemetry-driven).

## RP-P4 teardown: both providers 0/0, deadman disarmed, $0 CONFIRMED.
