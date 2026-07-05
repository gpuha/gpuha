"""Provider adapters. Registry maps a topology `provider` string to a class."""
from .base import ProviderAdapter, CapacityError, AdapterError

def get_adapter(provider: str, **kw) -> ProviderAdapter:
    if provider == "fake":
        from .fake import FakeAdapter
        return FakeAdapter(**kw)
    if provider == "gcp_stub":
        from .gcp_stub import GCPStubAdapter
        return GCPStubAdapter(**kw)
    if provider == "lambda":
        from .lambda_gpu import LambdaAdapter
        return LambdaAdapter(**kw)
    if provider == "runpod":
        from .runpod_gpu import RunPodAdapter
        return RunPodAdapter(**kw)
    raise AdapterError("unknown provider: %s" % provider)
