import sys,time,tempfile
sys.path.insert(0,".")
from orchestrator.topology import load_topology
from orchestrator.engine import Orchestrator
from orchestrator.wiring import FakeWiring
from orchestrator.adapters.fake import FakeAdapter
def topo(y):
    f=tempfile.NamedTemporaryFile("w",suffix=".yaml",delete=False); f.write(y); f.close(); return load_topology(f.name)
def W(): return FakeWiring(tempfile.mktemp(suffix=".map"))
def pool(n,extra=""): return "  - name: %s\n    provider: fake\n    role: stub\n%s"%(n,extra)
def yml(name,mp,pools): return "name: %s\nmin_pools: %d\nplane:\n  host: 127.0.0.1\npools:\n%s"%(name,mp,"\n".join(pools))
st=Orchestrator(topo(yml("A",2,[pool("p1"),pool("p2")])),W(),runs_dir=tempfile.mkdtemp()).up()
print("A status=%s pools=%s resources=%d"%(st.status,dict(st.pools),len(st.resources))); assert st.status=="up" and len(st.pools)==2,"A"
st=Orchestrator(topo(yml("B",2,[pool("bad","    fail: acquire\n"),pool("g1"),pool("g2")])),W(),runs_dir=tempfile.mkdtemp()).up()
print("B status=%s pools=%s bad-isolated=%s"%(st.status,dict(st.pools),"bad" not in st.pools)); assert st.status=="up" and len(st.pools)==2 and "bad" not in st.pools,"B"
orig=FakeAdapter.bootstrap; FakeAdapter.bootstrap=lambda self,h,role:(time.sleep(1.0),orig(self,h,role))[1]
t0=time.time(); st=Orchestrator(topo(yml("C",3,[pool("c1"),pool("c2"),pool("c3")])),W(),runs_dir=tempfile.mkdtemp()).up(); dt=time.time()-t0
FakeAdapter.bootstrap=orig
print("C 3 pools x1s bootstrap wall=%.2fs (sequential ~3s)"%dt); assert st.status=="up" and dt<1.8,"C not concurrent %.2f"%dt
print("OFFLINE ENGINE TESTS PASS: concurrency + per-pool isolation + thread-safe state")
