"""Offline proof of the whole orchestrator loop with the fake provider.
No cloud, no spend. Exercises the teardown-first invariants."""
import os, sys, json, tempfile, shutil
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from orchestrator.topology import load_topology, Topology, PoolSpec
from orchestrator.engine import Orchestrator
from orchestrator.wiring import FakeWiring
from orchestrator.adapters.fake import FakeAdapter
from orchestrator.adapters.base import AdapterError
from orchestrator import state as state_mod

RESULTS = []
def check(name, cond):
    RESULTS.append((name, bool(cond)))
    print(("  PASS " if cond else "  FAIL ") + name)


def make(tmp, pools, min_pools=None):
    topo = Topology(name="t", min_pools=min_pools or len(pools),
                    plane={"host": "1.1.1.1", "pool_map_file": tmp + "/pools.map"},
                    pools=pools)
    cloud = tmp + "/cloud.json"
    wiring = FakeWiring(tmp + "/pools.map")
    fac = lambda prov: FakeAdapter(cloud_file=cloud)
    orch = Orchestrator(topo, wiring, runs_dir=tmp + "/runs",
                        adapter_factory=fac, verify_fabric=lambda st: True)
    return topo, cloud, wiring, orch


def cloud_ids(cloud):
    return list(json.load(open(cloud)).keys()) if os.path.exists(cloud) else []


def t_happy():
    tmp = tempfile.mkdtemp()
    try:
        pools = [PoolSpec(name="gpuha-target-gcp-stub", provider="fake", role="stub")]
        topo, cloud, wiring, orch = make(tmp, pools)
        st = orch.up()
        check("up -> status up", st.status == "up")
        check("1 resource created on provider", len(cloud_ids(cloud)) == 1)
        check("pool wired into tier1 map", "gpuha-target-gcp-stub" in wiring.pools)
        check("pool-map file written", os.path.exists(tmp + "/pools.map"))
        check("firewall gate recorded", len(wiring.firewall_calls) == 1)
        check("state file persisted", os.path.exists(st.path))
        orch.down(st)
        check("down -> status down", st.status == "down")
        check("provider empty after down", cloud_ids(cloud) == [])
        check("pool unwired after down", wiring.pools == {})
        killed = orch.reap(run_id=st.run_id)
        check("reap after clean down finds nothing", killed == {})
    finally:
        shutil.rmtree(tmp)


def t_persist_before_create():
    tmp = tempfile.mkdtemp()
    try:
        pools = [PoolSpec(name="p1", provider="fake", role="stub"),
                 PoolSpec(name="p2", provider="fake", role="stub",
                          extra={"fail": "acquire"})]
        topo, cloud, wiring, orch = make(tmp, pools, min_pools=2)
        raised = False
        try:
            orch.up()
        except AdapterError:
            raised = True
        check("up raised on p2 hard failure", raised)
        runs = state_mod.list_runs(tmp + "/runs")
        st = state_mod.RunState.load_run(runs[-1], tmp + "/runs")
        had_p1 = any(r.pool == "p1" for r in st.resources)
        check("p1 recorded in persisted state before failure", had_p1)
        check("partial fabric torn down (provider empty)", cloud_ids(cloud) == [])
        check("status failed after quorum/hard fail", st.status == "failed")
    finally:
        shutil.rmtree(tmp)


def t_quorum_capacity():
    tmp = tempfile.mkdtemp()
    try:
        pools = [PoolSpec(name="p1", provider="fake", role="stub"),
                 PoolSpec(name="p2", provider="fake", role="stub",
                          extra={"fail": "capacity"})]
        topo, cloud, wiring, orch = make(tmp, pools, min_pools=2)
        raised = False
        try:
            orch.up()
        except AdapterError as e:
            raised = "quorum" in str(e)
        check("quorum failure raised", raised)
        check("no orphan left after quorum abort", cloud_ids(cloud) == [])
    finally:
        shutil.rmtree(tmp)


def t_capacity_tolerated():
    tmp = tempfile.mkdtemp()
    try:
        pools = [PoolSpec(name="p1", provider="fake", role="stub"),
                 PoolSpec(name="p2", provider="fake", role="stub",
                          extra={"fail": "capacity"})]
        topo, cloud, wiring, orch = make(tmp, pools, min_pools=1)
        st = orch.up()
        check("capacity-out tolerated when quorum holds", st.status == "up")
        check("only survivor wired", list(wiring.pools.keys()) == ["p1"])
        orch.down(st)
    finally:
        shutil.rmtree(tmp)


def t_orphan_reap():
    tmp = tempfile.mkdtemp()
    try:
        cloud = tmp + "/cloud.json"
        ad = FakeAdapter(cloud_file=cloud)
        ad.acquire(PoolSpec(name="orphan", provider="fake"), "ORPHANRUN-abc123")
        check("orphan exists on provider", len(cloud_ids(cloud)) == 1)
        topo = Topology(name="t", min_pools=1, plane={},
                        pools=[PoolSpec(name="orphan", provider="fake")])
        wiring = FakeWiring(tmp + "/pools.map")
        orch = Orchestrator(topo, wiring, runs_dir=tmp + "/runs",
                            adapter_factory=lambda p: FakeAdapter(cloud_file=cloud))
        killed = orch.reap(run_id=None)
        check("reap caught the orphan", bool(killed) and cloud_ids(cloud) == [])
    finally:
        shutil.rmtree(tmp)


if __name__ == "__main__":
    for t in (t_happy, t_persist_before_create, t_quorum_capacity,
              t_capacity_tolerated, t_orphan_reap):
        print("\n== %s ==" % t.__name__)
        t()
    n = len(RESULTS); ok = sum(1 for _, c in RESULTS if c)
    print("\n==== %d/%d checks passed ====" % (ok, n))
    sys.exit(0 if ok == n else 1)
