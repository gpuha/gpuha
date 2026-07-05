"""
vLLM telemetry shim: scrape a real vLLM worker's Prometheus /metrics + /health,
map into our TelemetryFrame, emit UDP to the router. Makes a stock vLLM worker
appear in the router's eligible set exactly like a native emitter would.

Run on the router box (or anywhere that can reach the worker + the router UDP port).
"""
import asyncio, argparse, time, urllib.request, re, socket
from telemetry import TelemetryFrame

def scrape_metrics(base_url, timeout=0.8):  # keep well under router freshness window (3.0s)
    """Return dict of vLLM metric_name -> float from the Prometheus endpoint."""
    out = {}
    try:
        with urllib.request.urlopen(f"{base_url}/metrics", timeout=timeout) as r:
            text = r.read().decode("utf-8", "replace")
    except Exception:
        return None  # unreachable -> caller stops emitting -> router prunes it
    for line in text.splitlines():
        if line.startswith("#") or " " not in line:
            continue
        name, _, val = line.partition(" ")
        # strip label braces for a coarse name match
        base = name.split("{")[0]
        try:
            out.setdefault(base, float(val))
        except ValueError:
            pass
    return out

def derive(metrics):
    """Map raw vLLM metrics to our frame fields. Best-effort; vLLM metric names
    vary by version, so we probe a few known ones and fall back sensibly."""
    # GPU KV cache utilization: strong proxy for 'how loaded is this worker'.
    vram = 0.0
    for k in ("vllm:kv_cache_usage_perc", "vllm:gpu_cache_usage_perc", "vllm:gpu_cache_usage_percentage"):
        if k in metrics:
            vram = metrics[k]
            if vram > 1.0: vram /= 100.0
            break
    # Queue depth: requests waiting in the scheduler.
    queue = 0
    for k in ("vllm:num_requests_waiting",):
        if k in metrics:
            queue = int(metrics[k]); break
    # TTFT: vLLM exposes a histogram; the _sum/_count give a running mean (secs).
    ttft_ms = 100.0
    s = metrics.get("vllm:time_to_first_token_seconds_sum")
    c = metrics.get("vllm:time_to_first_token_seconds_count")
    if s is not None and c and c > 0:
        ttft_ms = (s / c) * 1000.0
    return vram, ttft_ms, queue

async def run(worker_url, node_id, dests, backend, region, model, interval):
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    seq = int(time.time())  # monotonic across restarts (router drops seq<=last_seq)
    print(f"shim: scraping {worker_url}/metrics -> UDP {dests} as node '{node_id}'")
    try:
        while True:
            metrics = scrape_metrics(worker_url)
            if metrics is not None:
                vram, ttft_ms, queue = derive(metrics)
                seq += 1
                frame = TelemetryFrame(node_id=node_id, backend=backend, region=region,
                    ts_unix=time.time(), seq=seq, vram_used_frac=vram, ttft_ms=ttft_ms,
                    queue_depth=queue, price_usd_hr=0.71, model=model)
                for d in dests:
                    sock.sendto(frame.to_bytes(), d)
            # if metrics is None (worker unreachable) we intentionally emit NOTHING
            # -> router freshness gate prunes it. Silence = death.
            await asyncio.sleep(interval)
    finally:
        sock.close()

if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--worker-url", required=True, help="e.g. http://10.142.0.6:8000")
    ap.add_argument("--node-id", required=True, help="e.g. gpuha-w1")
    ap.add_argument("--dest", action="append", required=True,
                    help="router UDP host:port (repeatable = fan-out), e.g. 127.0.0.1:5006")
    ap.add_argument("--backend", default="gpuha-target-gcp-east")
    ap.add_argument("--region", default="us-east1")
    ap.add_argument("--model", default="gpuha")
    ap.add_argument("--interval", type=float, default=1.0)
    args = ap.parse_args()
    dests = [(h, int(p)) for h, p in (d.split(":") for d in args.dest)]
    asyncio.run(run(args.worker_url, args.node_id, dests, args.backend, args.region, args.model, args.interval))
