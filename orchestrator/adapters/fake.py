"""In-memory / file-backed fake provider for OFFLINE proof of the whole loop.

The 'cloud' is a JSON file (fake_cloud.json) acting as the provider's API: it is
the independent source of truth reap() queries, deliberately separate from the
orchestrator's run-state. This lets us test the crucial property that reap finds
orphans the local state never recorded.

Fault injection via pool_spec.extra:
  fail: capacity  -> acquire raises CapacityError
  fail: acquire   -> acquire raises AdapterError (hard failure)
"""
import json, os, time, threading
from .base import ProviderAdapter, CapacityError, AdapterError
from ..state import ResourceHandle

# FakeAdapter's 'cloud' is a shared JSON file -> serialize RMW for concurrent bring-up (offline test).
_FAKE_LOCK = threading.RLock()
def _synced(fn):
    def _w(*a, **k):
        with _FAKE_LOCK:
            return fn(*a, **k)
    return _w


class FakeAdapter(ProviderAdapter):
    name = "fake"

    def __init__(self, cloud_file=None, **kw):
        self.cloud_file = cloud_file or os.environ.get(
            "GPUHA_FAKE_CLOUD", "/tmp/gpuha_fake_cloud.json")

    def _load(self):
        if not os.path.exists(self.cloud_file):
            return {}
        with open(self.cloud_file) as f:
            return json.load(f)

    def _save(self, d):
        tmp = self.cloud_file + ".tmp"
        with open(tmp, "w") as f:
            json.dump(d, f, indent=2)
        os.replace(tmp, self.cloud_file)

    @_synced
    def acquire(self, pool_spec, run_id):
        fail = (pool_spec.extra or {}).get("fail")
        if fail == "capacity":
            raise CapacityError("fake: no capacity for %s" % pool_spec.name)
        if fail == "acquire":
            raise AdapterError("fake: hard acquire failure for %s" % pool_spec.name)
        cloud = self._load()
        handles = []
        for w in range(pool_spec.workers):
            rid = "fake-%s-%s-%d" % (pool_spec.name, run_id[-6:], w)
            ip = "10.99.%d.%d" % (len(cloud) % 256, w + 1)
            cloud[rid] = {
                "tag": self.tag_for(run_id), "run_id": run_id,
                "pool": pool_spec.name, "ip": ip, "region": pool_spec.region,
                "created_at": time.time(),
            }
            handles.append(ResourceHandle(
                provider="fake", kind="instance", id=rid, pool=pool_spec.name,
                role=pool_spec.role, region=pool_spec.region, public_ip=ip,
                tag=self.tag_for(run_id)))
        self._save(cloud)
        return handles

    @_synced
    def bootstrap(self, handle, role):
        cloud = self._load()
        if handle.id in cloud:
            cloud[handle.id]["bootstrapped"] = role
            self._save(cloud)

    def verify(self, handle):
        cloud = self._load()
        return handle.id in cloud

    @_synced
    def teardown(self, handle):
        cloud = self._load()
        if handle.id in cloud:
            del cloud[handle.id]
            self._save(cloud)

    @_synced
    def reap(self, run_id):
        cloud = self._load()
        killed = []
        for rid in list(cloud.keys()):
            rec = cloud[rid]
            if rec.get("tag", "").startswith("gpuha-managed=") and (
                    run_id is None or rec.get("run_id") == run_id):
                del cloud[rid]
                killed.append(rid)
        self._save(cloud)
        return killed
