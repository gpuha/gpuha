"""The acquire-with-fallback state machine. Teardown-first by construction."""
import os, time, threading
from .state import RunState, new_run_id
from .topology import Topology
from .adapters import get_adapter
from .adapters.base import CapacityError, AdapterError


def _log(msg):
    print("[gpuha] %s" % msg, flush=True)


class Orchestrator:
    def __init__(self, topology: Topology, wiring, runs_dir=None,
                 adapter_factory=get_adapter, verify_fabric=None):
        self.topo = topology
        self.wiring = wiring
        self.runs_dir = runs_dir
        self.adapter_factory = adapter_factory
        self.verify_fabric = verify_fabric
        self._adapters = {}
        self._wiring_lock = threading.Lock()

    def _adapter(self, provider):
        if provider not in self._adapters:
            self._adapters[provider] = self.adapter_factory(provider)
        return self._adapters[provider]

    def up(self) -> RunState:
        run_id = new_run_id()
        st = RunState(run_id, self.topo.name, self.runs_dir) if self.runs_dir \
            else RunState(run_id, self.topo.name)
        st.save()                       # PERSIST BEFORE ANY CREATE
        st.set_status("initializing")
        _log("run %s | topology %s | min_pools %d" % (run_id, self.topo.name, self.topo.min_pools))

        # CONCURRENT bring-up: one thread per pool, per-pool isolation. A pool's
        # failure must NEVER abort siblings; quorum is evaluated after all join.
        results = {}   # pool.name -> ("ok",ip) | ("capacity",msg) | ("error",msg)
        def _bring_up(pool):
            try:
                ad = self._adapter(pool.provider)
                _log("acquiring pool %s via %s ..." % (pool.name, pool.provider))
                try:
                    handles = ad.acquire(pool, run_id)
                except CapacityError as e:
                    st.event("CAPACITY-OUT %s (%s): %s" % (pool.name, pool.provider, e))
                    _log("capacity-out on %s: %s (continuing)" % (pool.name, e))
                    results[pool.name] = ("capacity", str(e)); return
                for h in handles:
                    st.add_resource(h)
                ip = None
                for h in handles:
                    with self._wiring_lock:
                        self.wiring.firewall_note(h.public_ip)
                    ad.bootstrap(h, pool.role)
                    st.update_resource(h.id, state="bootstrapped")
                    if not ad.verify(h):
                        raise AdapterError("verify failed for %s" % h.id)
                    st.update_resource(h.id, state="verified")
                    ip = ip or h.public_ip
                st.set_pool(pool.backend, ip)
                results[pool.name] = ("ok", ip)
            except Exception as e:
                st.event("POOL-FAILED %s: %s: %s" % (pool.name, type(e).__name__, e))
                _log("pool %s FAILED: %s (isolated; siblings continue)" % (pool.name, e))
                results[pool.name] = ("error", str(e))

        threads = [threading.Thread(target=_bring_up, args=(p,), name="up-" + p.name)
                   for p in self.topo.pools]
        serial = bool(os.environ.get("GPUHA_SERIAL"))   # timing comparison / debug
        for t in threads:
            t.start()
            if serial:
                t.join()
        if not serial:
            for t in threads:
                t.join()
        acquired_pools = sum(1 for r in results.values() if r[0] == "ok")
        _log("bring-up joined: %d/%d pools ok | %s"
             % (acquired_pools, len(self.topo.pools), {k: v[0] for k, v in results.items()}))

        torn = False
        try:
            if acquired_pools < self.topo.min_pools:
                st.set_status("aborting")
                _log("QUORUM FAIL: %d < min_pools %d -> tearing down partial fabric"
                     % (acquired_pools, self.topo.min_pools))
                self._teardown_all(st)
                torn = True
                st.set_status("failed")
                raise AdapterError("quorum not met (%d/%d); partial fabric torn down"
                                   % (acquired_pools, self.topo.min_pools))

            for name, ip in st.pools.items():
                self.wiring.add_pool(name, ip)
            self.wiring.apply()
            st.event("tier1 wired: %s" % st.pools)

            if self.verify_fabric:
                ok = self.verify_fabric(st)
                st.event("fabric verify: %s" % ("OK" if ok else "FAIL"))
                if not ok:
                    st.set_status("aborting")
                    self._teardown_all(st)
                    torn = True
                    st.set_status("failed")
                    raise AdapterError("fabric verify failed; torn down")

            st.set_status("up")
            _log("UP: %d pool(s) live: %s" % (acquired_pools, st.pools))
            return st
        except Exception as e:
            st.event("UP-FAILURE %s: %s" % (type(e).__name__, e))
            if not torn:
                st.set_status("aborting")
                try:
                    self._teardown_all(st)
                except Exception as te:
                    st.event("teardown-during-abort error: %s (reap is the backstop)" % te)
                st.set_status("failed")
            _log("up failed: %s -> partial fabric torn down (verify with `gpuha reap`)" % e)
            raise

    def down(self, st: RunState):
        st.set_status("aborting")
        self._teardown_all(st)
        for name in list(st.pools.keys()):
            self.wiring.remove_pool(name)
            st.unset_pool(name)
        self.wiring.apply()
        st.set_status("down")
        _log("DOWN: run %s torn down; pools unwired" % st.run_id)
        return st

    def _teardown_all(self, st: RunState):
        for r in st.active_resources():
            ad = self._adapter(r.provider)
            try:
                ad.teardown(r)
                st.update_resource(r.id, state="torn_down")
                _log("torn down %s/%s" % (r.provider, r.id))
            except Exception as e:
                st.event("TEARDOWN-ERROR %s: %s" % (r.id, e))
                _log("teardown error %s: %s (reap will catch it)" % (r.id, e))

    def reap(self, run_id=None, providers=None):
        """Query each provider for gpuha-managed resources and destroy them,
        independent of local state. `providers` defaults to those in topology."""
        provs = providers or sorted({p.provider for p in self.topo.pools})
        killed = {}
        for prov in provs:
            try:
                ad = self._adapter(prov)
                k = ad.reap(run_id)
            except Exception as e:
                _log("reap: provider %s skipped (%s)" % (prov, e))
                continue
            if k:
                killed[prov] = k
                _log("reaped %s: %s" % (prov, k))
        if not killed:
            _log("reap: nothing tagged gpuha-managed found (clean)")
        return killed
