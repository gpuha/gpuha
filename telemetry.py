from __future__ import annotations
from dataclasses import dataclass, asdict
import json, time

FRAME_VERSION = 1


@dataclass
class TelemetryFrame:
    node_id: str
    backend: str
    region: str
    ts_unix: float
    seq: int
    vram_used_frac: float
    ttft_ms: float
    queue_depth: int
    price_usd_hr: float
    model: str = ""
    max_concurrency: int = 0
    v: int = FRAME_VERSION

    def to_bytes(self) -> bytes:
        return json.dumps(asdict(self), separators=(",", ":")).encode("utf-8")

    @staticmethod
    def from_bytes(data: bytes) -> "TelemetryFrame":
        obj = json.loads(data.decode("utf-8"))
        ver = obj.get("v")
        if ver is None:
            raise ValueError("telemetry frame missing version field 'v'")
        if ver > FRAME_VERSION:
            raise ValueError(f"frame version {ver} > supported {FRAME_VERSION}")
        known = TelemetryFrame.__dataclass_fields__.keys()
        return TelemetryFrame(**{k: val for k, val in obj.items() if k in known})


@dataclass
class NodeView:
    frame: TelemetryFrame
    received_monotonic: float
    last_seq: int

    def is_fresh(self, now_monotonic, max_age_s):
        return (now_monotonic - self.received_monotonic) <= max_age_s


class TelemetryIngest:
    def __init__(self):
        self.nodes: dict[str, NodeView] = {}
        self.dropped_frames: dict[str, int] = {}

    def ingest(self, data, now_monotonic=None):
        now = now_monotonic if now_monotonic is not None else time.monotonic()
        try:
            frame = TelemetryFrame.from_bytes(data)
        except (ValueError, json.JSONDecodeError):
            return None
        prev = self.nodes.get(frame.node_id)
        if prev is not None:
            gap = frame.seq - prev.last_seq - 1
            if gap > 0:
                self.dropped_frames[frame.node_id] = self.dropped_frames.get(frame.node_id, 0) + gap
            if frame.seq <= prev.last_seq:
                return prev
        view = NodeView(frame=frame, received_monotonic=now, last_seq=frame.seq)
        self.nodes[frame.node_id] = view
        return view

    def fresh_nodes(self, max_age_s, now_monotonic=None):
        now = now_monotonic if now_monotonic is not None else time.monotonic()
        return [v for v in self.nodes.values() if v.is_fresh(now, max_age_s)]

    def fresh_backends(self, max_age_s, now_monotonic=None):
        return {v.frame.backend for v in self.fresh_nodes(max_age_s, now_monotonic)}
