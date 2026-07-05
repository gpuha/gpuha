from __future__ import annotations
import time, random
from dataclasses import dataclass, field
from typing import Optional

@dataclass
class SelectorConfig:
    telemetry_fresh_secs: float = 3.0
    breaker_cooldown_secs: float = 5.0
    w_ttft: float = 0.45
    w_vram_headroom: float = 0.35
    w_queue: float = 0.15
    w_price: float = 0.05
    top_n: int = 2
    ttft_ref_ms: float = 2000.0
    queue_ref: float = 32.0
    price_ref: float = 5.0

@dataclass
class Worker:
    id: str
    vram_used_frac: float = 0.0
    ttft_ms: float = 100.0
    queue_depth: int = 0
    price_per_hr: float = 1.0
    last_seen: float = field(default_factory=time.monotonic)
    broken_until: float = 0.0
    def is_fresh(self, now, max_age): return (now - self.last_seen) <= max_age
    def is_broken(self, now): return now < self.broken_until

class WorkerSelector:
    def __init__(self, config=None, rng=None):
        self.cfg = config or SelectorConfig()
        self.workers: dict[str, Worker] = {}
        self._rng = rng or random.Random()
    def upsert(self, w): self.workers[w.id] = w
    def update_telemetry(self, worker_id, *, vram_used_frac=None, ttft_ms=None,
                         queue_depth=None, price_per_hr=None, now=None):
        w = self.workers.get(worker_id)
        if w is None: return
        if vram_used_frac is not None: w.vram_used_frac = vram_used_frac
        if ttft_ms is not None: w.ttft_ms = ttft_ms
        if queue_depth is not None: w.queue_depth = queue_depth
        if price_per_hr is not None: w.price_per_hr = price_per_hr
        w.last_seen = now if now is not None else time.monotonic()
    def eligible(self, now):
        out = []
        for w in self.workers.values():
            if not w.is_fresh(now, self.cfg.telemetry_fresh_secs): continue
            if w.is_broken(now): continue
            out.append(w)
        return out
    def score(self, w):
        cfg = self.cfg
        s_vram = max(0.0, 1.0 - w.vram_used_frac)
        s_ttft = max(0.0, 1.0 - (w.ttft_ms / cfg.ttft_ref_ms))
        s_queue = max(0.0, 1.0 - (w.queue_depth / cfg.queue_ref))
        s_price = max(0.0, 1.0 - (w.price_per_hr / cfg.price_ref))
        tw = cfg.w_ttft + cfg.w_vram_headroom + cfg.w_queue + cfg.w_price
        raw = (cfg.w_ttft*s_ttft + cfg.w_vram_headroom*s_vram +
               cfg.w_queue*s_queue + cfg.w_price*s_price)
        return raw/tw if tw else 0.0
    def select(self, now=None, exclude=None):
        now = now if now is not None else time.monotonic()
        exclude = exclude or set()
        pool = [w for w in self.eligible(now) if w.id not in exclude]
        if not pool: return None
        ranked = sorted(pool, key=self.score, reverse=True)
        top = ranked[:self.cfg.top_n]
        weights = [(self.score(w) + 1e-6) ** 2 for w in top]
        return self._rng.choices(top, weights=weights, k=1)[0]
    def trip_breaker(self, worker_id, now=None):
        now = now if now is not None else time.monotonic()
        w = self.workers.get(worker_id)
        if w: w.broken_until = now + self.cfg.breaker_cooldown_secs
    def select_with_retry_plan(self, max_attempts=3, now=None):
        now = now if now is not None else time.monotonic()
        plan, tried = [], set()
        for _ in range(max_attempts):
            w = self.select(now=now, exclude=tried)
            if w is None: break
            plan.append(w); tried.add(w.id)
        return plan
