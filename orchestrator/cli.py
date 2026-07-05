"""`gpuha` CLI: up / down / reap / status. Runs on the plane."""
import argparse, os, socket, subprocess, sys, time
from . import state as state_mod
from .topology import load_topology, COLD_WARMUP_DEFAULT
from .engine import Orchestrator
from .wiring import RealWiring


def _verify_budget(topo):
    # per-pool warmup_budget wins; else plane verify_timeout; else cold-GPU default
    default_budget = int(topo.plane.get("verify_timeout", COLD_WARMUP_DEFAULT))
    return max((p.warmup_budget if p.warmup_budget > 0 else default_budget
                for p in topo.pools), default=default_budget)


def _dig_check(topo, timeout=210, interval=5):
    def check(st):
        port = str(topo.plane.get("tier1_dns_port", 5353))
        deadline = time.time() + timeout
        while True:
            try:
                out = subprocess.run(
                    ["dig", "@127.0.0.1", "-p", port, "api.gpuha.com", "+short"],
                    capture_output=True, text=True, timeout=10).stdout
            except Exception:
                out = ""
            answered = set(out.split())
            ok = all(ip in answered for ip in st.pools.values())
            if ok:
                print("[gpuha] dig answer=%s expected=%s -> OK" % (sorted(answered), st.pools))
                return True
            if time.time() >= deadline:
                print("[gpuha] dig answer=%s expected=%s -> FAIL (timeout %ss)" % (sorted(answered), st.pools, timeout))
                return False
            print("[gpuha] waiting for pool to warm up... dig=%s" % sorted(answered))
            time.sleep(interval)
    return check


def _adapter_factory(topo):
    from .adapters import get_adapter
    def factory(provider):
        if provider == "gcp_stub":
            from .adapters.gcp_stub import GCPStubAdapter
            return GCPStubAdapter(plane_ip=topo.plane_ip, telem_port=topo.telem_port,
                                  http_port=int(topo.plane.get("http_port", 8090)))
        if provider == "lambda":
            from .adapters.lambda_gpu import LambdaAdapter
            return LambdaAdapter(plane_ip=topo.plane_ip, telem_port=topo.telem_port,
                                 http_port=int(topo.plane.get("http_port", 8090)),
                                 instance_type=topo.plane.get("instance_type", "gpu_1x_a10"))
        return get_adapter(provider)
    return factory


def _ensure_fileserver(topo):
    port = str(topo.plane.get("http_port", 8090))
    r = subprocess.run(["pgrep", "-f", "http.server %s" % port], capture_output=True, text=True)
    if not r.stdout.strip():
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        subprocess.Popen(["python3", "-m", "http.server", port], cwd=root,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                         stdin=subprocess.DEVNULL)
        time.sleep(1)
        print("[gpuha] started file-server on :%s (%s)" % (port, root))


def _lambda_fabric_check(topo, orch, timeout=900, interval=10):
    port = str(topo.plane.get("tier1_dns_port", 5353))
    def check(st):
        deadline = time.time() + timeout
        while True:
            try:
                out = subprocess.run(["dig", "@127.0.0.1", "-p", port, "api.gpuha.com", "+short"],
                                     capture_output=True, text=True, timeout=10).stdout
            except Exception:
                out = ""
            answered = set(out.split())
            if st.pools and all(ip in answered for ip in st.pools.values()):
                print("[gpuha] GPU pool live in DNS: %s" % st.pools); break
            if time.time() >= deadline:
                print("[gpuha] dig timeout; GPU pool never warmed (answer=%s)" % sorted(answered)); return False
            print("[gpuha] waiting for GPU pool to warm (vLLM cold start)... dig=%s" % sorted(answered))
            time.sleep(interval)
        gpu = next((r for r in st.resources if r.role == "gpu" and r.state != "torn_down"), None)
        if not gpu:
            print("[gpuha] no gpu handle in state"); return False
        ad = orch._adapter("lambda")
        out = ""
        for _ in range(12):
            out = ad.completion(gpu)
            if ("x-gpuha-served-by" in out.lower()) and ("HTTP=200" in out):
                print("[gpuha] --- real streamed completion through the pool router ---")
                print(out)
                print("[gpuha] real-completion proof: OK")
                return True
            time.sleep(8)
        print("[gpuha] --- completion FAILED after retries; last output ---")
        print(out)
        print("[gpuha] --- instance diagnostics (router.log etc) ---")
        print(ad.diag(gpu))
        return False
    return check


def cmd_up(args):
    topo = load_topology(args.topology)
    _ensure_fileserver(topo)
    wiring = RealWiring(topo.pool_map_file, topo.plane.get("tier1_service", "gpuha-tier1"))
    orch = Orchestrator(topo, wiring, adapter_factory=_adapter_factory(topo))
    if any(p.provider == "lambda" for p in topo.pools):
        orch.verify_fabric = _lambda_fabric_check(topo, orch,
                                 timeout=_verify_budget(topo))
    else:
        orch.verify_fabric = _dig_check(topo)
    st = orch.up()
    print("run-id:", st.run_id, "| status:", st.status)


def cmd_down(args):
    st = state_mod.RunState.load_run(args.run_id) if args.run_id else _latest()
    topo = load_topology(args.topology) if args.topology else None
    pmf = topo.pool_map_file if topo else "~/gpuha-runs/tier1-pools.map"
    svc = topo.plane.get("tier1_service", "gpuha-tier1") if topo else "gpuha-tier1"
    wiring = RealWiring(pmf, svc)
    orch = Orchestrator(topo, wiring, adapter_factory=_adapter_factory(topo)) if topo \
        else Orchestrator(_shim_topo(st), wiring)
    orch.down(st)


# providers to sweep when `reap --all` is called with no topology/providers context
KNOWN_PROVIDERS = ["lambda", "runpod", "gcp_stub"]


def _adapter_factory_default():
    from .adapters import get_adapter
    return lambda provider: get_adapter(provider)  # reads creds from env (.env)


def cmd_reap(args):
    provs = args.providers.split(",") if args.providers else None
    topo = load_topology(args.topology) if args.topology else None
    pmf = topo.pool_map_file if topo else "~/gpuha-runs/tier1-pools.map"
    wiring = RealWiring(pmf)
    factory = _adapter_factory(topo) if topo else _adapter_factory_default()
    orch = Orchestrator(topo or _shim_topo(None), wiring, adapter_factory=factory)
    if provs is None and topo is None:
        provs = list(KNOWN_PROVIDERS)  # bare `reap --all` sweeps ALL known providers
    killed = orch.reap(run_id=(None if args.all else args.run_id), providers=provs)
    print("reaped:", killed if killed else "nothing")


def cmd_status(args):
    for rid in state_mod.list_runs():
        st = state_mod.RunState.load_run(rid)
        print("%s  %-12s pools=%s resources=%d"
              % (rid, st.status, st.pools, len(st.active_resources())))


def _latest():
    runs = state_mod.list_runs()
    if not runs:
        print("no runs found"); sys.exit(1)
    return state_mod.RunState.load_run(runs[-1])


def _shim_topo(st):
    from .topology import Topology
    return Topology(name=(st.topology_name if st else "unknown"),
                    min_pools=0, plane={}, pools=[])


def main(argv=None):
    ap = argparse.ArgumentParser(prog="gpuha", description="GPU HA orchestrator")
    sub = ap.add_subparsers(dest="cmd", required=True)
    up = sub.add_parser("up"); up.add_argument("topology"); up.set_defaults(fn=cmd_up)
    dn = sub.add_parser("down"); dn.add_argument("run_id", nargs="?")
    dn.add_argument("--topology"); dn.set_defaults(fn=cmd_down)
    rp = sub.add_parser("reap"); rp.add_argument("run_id", nargs="?")
    rp.add_argument("--all", action="store_true"); rp.add_argument("--providers")
    rp.add_argument("--topology"); rp.set_defaults(fn=cmd_reap)
    stt = sub.add_parser("status"); stt.set_defaults(fn=cmd_status)
    args = ap.parse_args(argv)
    args.fn(args)


if __name__ == "__main__":
    main()
