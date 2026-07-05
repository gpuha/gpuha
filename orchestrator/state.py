"""Run-state persistence. Teardown-first invariant: the state file is written
BEFORE the first resource is created and updated after every create, so a crash
mid-`up` still leaves an on-disk record `down`/`reap` can act on."""
import json, os, time, uuid, tempfile, threading
from dataclasses import dataclass, field, asdict

DEFAULT_RUNS_DIR = os.path.expanduser("~/gpuha-runs")


def new_run_id() -> str:
    # short, sortable-ish, unique enough for tagging cloud resources
    return time.strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:6]


@dataclass
class ResourceHandle:
    """One created cloud resource. `tag` is what reap matches on the provider."""
    provider: str
    kind: str                      # "instance"
    id: str                        # provider-native id/name
    pool: str                      # logical pool name (== tier1 backend)
    role: str                      # "stub" | "gpu"
    region: str = ""
    public_ip: str = ""
    tag: str = ""                  # gpuha-managed=<run-id>
    state: str = "created"         # created|bootstrapped|verified|torn_down
    extra: dict = field(default_factory=dict)

    def to_dict(self):
        return asdict(self)

    @staticmethod
    def from_dict(d):
        return ResourceHandle(**d)


class RunState:
    """JSON-backed. Every mutation flushes atomically to disk."""

    def __init__(self, run_id, topology_name, runs_dir=DEFAULT_RUNS_DIR):
        self.run_id = run_id
        self.topology_name = topology_name
        self.runs_dir = runs_dir
        self.created_at = time.time()
        self.status = "initializing"   # initializing|up|aborting|down|failed
        self.resources = []            # list[ResourceHandle]
        self.pools = {}                # name -> ip  (what is wired into tier1)
        self.events = []               # append-only audit log
        self._lock = threading.RLock()

    @property
    def path(self):
        return os.path.join(self.runs_dir, self.run_id + ".json")

    def to_dict(self):
        return {
            "run_id": self.run_id,
            "topology_name": self.topology_name,
            "created_at": self.created_at,
            "status": self.status,
            "resources": [r.to_dict() for r in list(self.resources)],
            "pools": dict(self.pools),
            "events": list(self.events),
        }

    def save(self):
        with self._lock:
            return self._save_locked()

    def _save_locked(self):
        os.makedirs(self.runs_dir, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=self.runs_dir, prefix=".tmp-", suffix=".json")
        try:
            with os.fdopen(fd, "w") as f:
                json.dump(self.to_dict(), f, indent=2, sort_keys=True)
                f.flush(); os.fsync(f.fileno())
            os.replace(tmp, self.path)
        finally:
            if os.path.exists(tmp):
                os.remove(tmp)
        return self.path

    @classmethod
    def load(cls, path, runs_dir=None):
        with open(path) as f:
            d = json.load(f)
        rs = cls(d["run_id"], d["topology_name"],
                 runs_dir or os.path.dirname(os.path.abspath(path)))
        rs.created_at = d.get("created_at", time.time())
        rs.status = d.get("status", "unknown")
        rs.resources = [ResourceHandle.from_dict(x) for x in d.get("resources", [])]
        rs.pools = d.get("pools", {})
        rs.events = d.get("events", [])
        return rs

    @classmethod
    def load_run(cls, run_id, runs_dir=DEFAULT_RUNS_DIR):
        return cls.load(os.path.join(runs_dir, run_id + ".json"), runs_dir)

    def event(self, msg):
        self.events.append({"ts": time.time(), "msg": msg})
        self.save()

    def set_status(self, status):
        self.status = status
        self.event("status -> %s" % status)

    def add_resource(self, handle: ResourceHandle):
        self.resources.append(handle)
        self.event("resource created: %s/%s pool=%s" % (handle.provider, handle.id, handle.pool))

    def update_resource(self, res_id, **kw):
        for r in self.resources:
            if r.id == res_id:
                for k, v in kw.items():
                    setattr(r, k, v)
                self.event("resource updated: %s -> %s" % (res_id, kw))
                return r
        raise KeyError(res_id)

    def drop_resource(self, res_id):
        self.resources = [r for r in self.resources if r.id != res_id]
        self.event("resource removed from state: %s" % res_id)

    def set_pool(self, name, ip):
        self.pools[name] = ip
        self.event("pool wired: %s=%s" % (name, ip))

    def unset_pool(self, name):
        self.pools.pop(name, None)
        self.event("pool unwired: %s" % name)

    def active_resources(self):
        return [r for r in self.resources if r.state != "torn_down"]


def list_runs(runs_dir=DEFAULT_RUNS_DIR):
    if not os.path.isdir(runs_dir):
        return []
    return sorted(f[:-5] for f in os.listdir(runs_dir)
                  if f.endswith(".json") and not f.startswith(".tmp"))
