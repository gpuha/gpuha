#!/usr/bin/env python3
"""Minimal telemetry emitter for the Phase D.5 distributed DNS drill.

Emits TelemetryFrames for one pool/node to one or more --dest (repeatable = fan-out,
the same pattern the shim/fake_worker need). Pure stdlib; runs on any box (no GPU).
Used as the stand-in pool emitter for both the Lambda pool and the GCP stub pool so
the drill does not depend on the full Tier-2 stack. seq is seeded from wall-clock so it
is monotonic across restarts (the router/tier1 ingest drops seq <= last_seq).

Example (GCP stub, cross-internet to Lambda tier1):
  python3 stub_emitter.py --node-id gcp-stub --backend gpuha-target-gcp-east \
      --region us-east1 --dest <LAMBDA_IP>:5106
"""
import socket, time, argparse
from telemetry import TelemetryFrame


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--node-id", required=True)
    ap.add_argument("--backend", required=True, help="pool name, e.g. gpuha-target-gcp-east")
    ap.add_argument("--region", default="unknown")
    ap.add_argument("--dest", action="append", default=[], required=True,
                    help="host:port (repeatable = fan-out)")
    ap.add_argument("--interval", type=float, default=1.0)
    ap.add_argument("--model", default="gpuha")
    a = ap.parse_args()
    dests = [(h, int(p)) for h, p in (d.split(":") for d in a.dest)]
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    seq = int(time.time())
    print("stub_emitter: node=%s backend=%s -> %s (seq0=%d)" % (a.node_id, a.backend, dests, seq),
          flush=True)
    while True:
        seq += 1
        f = TelemetryFrame(node_id=a.node_id, backend=a.backend, region=a.region,
                           ts_unix=time.time(), seq=seq, vram_used_frac=0.1, ttft_ms=40.0,
                           queue_depth=0, price_usd_hr=0.75, model=a.model)
        b = f.to_bytes()
        for d in dests:
            sock.sendto(b, d)
        time.sleep(a.interval)


if __name__ == "__main__":
    main()
