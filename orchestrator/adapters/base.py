"""Common adapter interface. Every provider knows its own billing-correct
teardown verb (the whole point of Phase O)."""
from ..state import ResourceHandle


class AdapterError(Exception):
    pass


class CapacityError(AdapterError):
    """Raised by acquire() when the provider has no capacity for this pool.
    The engine tolerates this if best-effort quorum still holds."""
    pass


class ProviderAdapter:
    name = "base"

    # each created resource is tagged so reap() can find it with no local state
    @staticmethod
    def tag_for(run_id: str) -> str:
        return "gpuha-managed=%s" % run_id

    def acquire(self, pool_spec, run_id) -> list:
        """Create instance(s) for a pool, tag them, return list[ResourceHandle].
        Raise CapacityError on capacity-out; other failures raise AdapterError."""
        raise NotImplementedError

    def bootstrap(self, handle: ResourceHandle, role: str) -> None:
        """SSH in and start the workload: stub emitter (role=stub) or
        vLLM+router+shim (role=gpu)."""
        raise NotImplementedError

    def verify(self, handle: ResourceHandle) -> bool:
        """Instance reachable / emitting."""
        raise NotImplementedError

    def teardown(self, handle: ResourceHandle) -> None:
        """Provider-correct destroy (delete/terminate per the billing matrix)."""
        raise NotImplementedError

    def reap(self, run_id: str) -> list:
        """Query the provider for anything tagged gpuha-managed=<run_id>
        (or all gpuha-managed if run_id is None), destroy it, return the ids.
        The backstop against a forgotten bill; independent of local state."""
        raise NotImplementedError
