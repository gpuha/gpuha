# Phase C'-a evidence: intra-pool real-GPU failover (Tier-2 L7 router)

Removes the last stub-asterisk from Phase A: the failover *target* is now a real
GPU-backed vLLM worker, not a protocol stub.

Setup: ONE Lambda A10 running TWO real vLLM workers (Qwen2.5-3B, memory-split:
gpu-mem-util 0.38, max-model-len 2048, max-num-seqs 8, ports 8000/8001) behind ONE
router (--backend gpuha-w1=...:8000 --backend gpuha-w2=...:8001), one shim per worker.

Drill (cprimea-drill.log):
- BEFORE: router eligible = [gpuha-w1, gpuha-w2]; requests served by BOTH.
- KILL gpuha-w1's vLLM (pkill -9 port 8000).
- AFTER: router eligible = [gpuha-w2] ONLY (w1 evicted on telemetry silence);
  all subsequent completions X-GPUHA-Served-By: gpuha-w2, real tokens ("GPU HA
  online"), HTTP 200, zero client errors. failovers/midstream_failfast handled by
  the first-token contract.

Caveat: two vLLM co-resident on one physical A10 (cost/reliability). The failover
mechanism + real-GPU-backed target are proven; two *physically separate* GPUs is the
trivial extension (two instances wired to one router, or a 2-GPU box).
