#!/usr/bin/env python3
"""GPU HA Tier-1: telemetry-driven authoritative DNS evacuation (D1).

Imports the SAME TelemetryIngest from telemetry.py that the L7 router uses: one
versioned telemetry contract drives BOTH tiers. No adapter/translator -- an adapter
is a new SPOF whose silent death would freeze DNS state, the exact failure this
project eliminates. The frame is the contract; consumers adapt to it.

- UDP DNS responder (A queries only; hand-rolled wire; TTL 0).
- UDP telemetry ingest (same TelemetryFrame / TelemetryIngest class).
- Pool (frame.backend) is IN the answer iff it has >=1 node with age <= TIER1_FRESH.
- Zero fresh pools -> FAILSAFE: answer the full configured set (never NXDOMAIN/empty).
- Logs every answer-set transition + per-node dropped_frames (UDP loss) with timestamps.
"""
import socket, struct, threading, time, argparse
import os, signal  # --pool-file + SIGHUP reload
from telemetry import TelemetryIngest   # shared contract, shared code

def _load_pool_file(path):
    """Read backend=ip lines (dynamic orchestrated pools). Missing file = {}."""
    pools = {}
    if not path:
        return pools
    try:
        with open(os.path.expanduser(path)) as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    k, _, v = line.partition("=")
                    pools[k.strip()] = v.strip()
    except FileNotFoundError:
        pass
    return pools


TIER1_FRESH        = 10.0     # pool eviction window (s); coarser than router's 3s node gate
DNS_PORT_DEFAULT   = 5353
TELEM_PORT_DEFAULT = 5106
ZONE               = "api.gpuha.com"


def log(msg):
    print("%s tier1: %s" % (time.strftime("%H:%M:%S"), msg), flush=True)


def parse_qname(data, off):
    labels = []
    while True:
        if off >= len(data):
            raise ValueError("qname overrun")
        n = data[off]; off += 1
        if n == 0:
            break
        if n & 0xC0:
            raise ValueError("compression in question")
        labels.append(data[off:off + n]); off += n
    return b".".join(labels).decode("latin1"), off


def build_response(query, answers, rcode=0):
    qid = int.from_bytes(query[:2], "big")
    _, qend = parse_qname(query, 12); qend += 4
    question = query[12:qend]
    rd = query[2] & 0x01
    flags = 0x8000 | 0x0400 | (rd << 8) | (rcode & 0x0F)
    out = struct.pack(">HHHHHH", qid, flags, 1, len(answers), 0, 0) + question
    for ip4 in answers:
        out += b"\xc0\x0c" + struct.pack(">HHIH", 1, 1, 0, 4) + ip4
    return out


class Tier1:
    def __init__(self, pool_map, fresh, failsafe_ip=None):
        self.pool_map = pool_map
        self.fresh = fresh
        self.failsafe_ip = failsafe_ip         # whale A record; when set, FAILSAFE answers this alone
        self.ingest = TelemetryIngest()
        self.last_answer = None
        self._lock = threading.Lock()

    def telem_loop(self, port):
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM); s.bind(("0.0.0.0", port))
        log("telemetry ingest on udp/%d (pool fresh window %.1fs)" % (port, self.fresh))
        while True:
            data, _ = s.recvfrom(65535)
            try:
                self.ingest.ingest(data)
            except Exception:
                pass

    def current_pools(self):
        fresh = self.ingest.fresh_backends(self.fresh)
        pools = [b for b in self.pool_map if b in fresh]
        failsafe = False
        if not pools:
            pools = list(self.pool_map.keys()); failsafe = True
        return pools, failsafe

    def answer_ips(self, pools, failsafe):
        # FAILSAFE with a configured whale -> answer the whale alone (graceful degradation).
        # Otherwise (normal, or FAILSAFE without a whale) -> the mapped pool IPs.
        if failsafe and self.failsafe_ip:
            return [self.failsafe_ip]
        return [self.pool_map[p] for p in pools]

    def note_transition(self, pools, failsafe):
        with self._lock:
            key = (tuple(pools), failsafe)
            if key != self.last_answer:
                ips = self.answer_ips(pools, failsafe)
                if failsafe and self.failsafe_ip:
                    tag = "  [FAILSAFE->WHALE %s]" % self.failsafe_ip
                elif failsafe:
                    tag = "  [FAILSAFE full-set]"
                else:
                    tag = ""
                log("ANSWER SET -> %s  pools=%s%s  dropped_frames=%s"
                    % (ips, pools, tag, dict(self.ingest.dropped_frames)))
                self.last_answer = key

    def monitor_loop(self):
        last_drop_log = 0.0
        while True:
            p, f = self.current_pools(); self.note_transition(p, f)
            now = time.monotonic()
            if now - last_drop_log >= 30.0:   # first real UDP-loss numbers of the project
                log("dropped_frames (per-node UDP loss)=%s" % dict(self.ingest.dropped_frames))
                last_drop_log = now
            time.sleep(1.0)

    def dns_loop(self, port):
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM); s.bind(("0.0.0.0", port))
        log("DNS responder on udp/%d for %s" % (port, ZONE))
        while True:
            data, addr = s.recvfrom(65535)
            try:
                resp = self.handle(data)
            except Exception as e:
                log("bad query from %s: %r" % (addr, e)); continue
            if resp:
                s.sendto(resp, addr)

    def handle(self, data):
        if len(data) < 12:
            return None
        qname, qend = parse_qname(data, 12)
        qtype, qclass = struct.unpack(">HH", data[qend:qend + 4])
        if qtype != 1:
            return build_response(data, [], rcode=4)
        pools, failsafe = self.current_pools()
        self.note_transition(pools, failsafe)
        answers = [socket.inet_aton(ip) for ip in self.answer_ips(pools, failsafe)]
        return build_response(data, answers, rcode=0)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dns-port", type=int, default=DNS_PORT_DEFAULT)
    ap.add_argument("--telem-port", type=int, default=TELEM_PORT_DEFAULT)
    ap.add_argument("--fresh", type=float, default=TIER1_FRESH, help="pool eviction window (s)")
    ap.add_argument("--pool", action="append", default=[], help="backend=ip (repeatable)")
    ap.add_argument("--failsafe-ip", default=None,
                    help="whale A record; when set, FAILSAFE answers this IP alone (graceful degradation)")
    ap.add_argument("--pool-file", default=None, help="backend=ip lines (dynamic pools; merged with --pool, re-read on SIGHUP)")
    a = ap.parse_args()
    pool_map = {}
    for p in a.pool:
        k, _, v = p.partition("="); pool_map[k] = v
    pool_map.update(_load_pool_file(a.pool_file))
    if not pool_map:
        pool_map = {"gpuha-target-gcp-east": "127.0.0.1", "gpuha-target-runpod": "127.0.0.2"}
    t = Tier1(pool_map, a.fresh, a.failsafe_ip)
    log("pools=%s fresh=%.1fs" % (pool_map, a.fresh))

    def _reload(_sig, _frm):
        merged = {}
        for _p in a.pool:
            _k, _, _v = _p.partition("="); merged[_k] = _v
        merged.update(_load_pool_file(a.pool_file))
        t.pool_map = merged
        log("SIGHUP reload -> pools=%s" % merged)
    signal.signal(signal.SIGHUP, _reload)
    threading.Thread(target=t.telem_loop, args=(a.telem_port,), daemon=True).start()
    threading.Thread(target=t.monitor_loop, daemon=True).start()
    t.dns_loop(a.dns_port)


if __name__ == "__main__":
    main()
