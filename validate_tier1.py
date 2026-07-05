#!/usr/bin/env python3
"""Local validation for tier1_dns.py -- proves telemetry-driven DNS evacuation
(baseline / evacuate / restore / FAILSAFE) with REAL DNS queries and REAL
TelemetryFrames on localhost. No cloud infra needed. Uses a short fresh window
so the whole run is a few seconds (production default is 10s).

Two pools:
  gpuha-target-gcp-east -> 10.0.0.1   (stand-in for GCP pool router IP)
  gpuha-target-runpod   -> 10.0.0.2   (stand-in for RunPod pool public IP)
"""
import socket, struct, subprocess, sys, time, os, threading

DNS_PORT, TELEM_PORT = 5353, 5106
FRESH = 2.5
GCP_IP, RP_IP = "10.0.0.1", "10.0.0.2"
GCP_BE, RP_BE = "gpuha-target-gcp-east", "gpuha-target-runpod"

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from telemetry import TelemetryFrame


def emit(node_id, backend, seq):
    f = TelemetryFrame(node_id=node_id, backend=backend, region="r", ts_unix=time.time(),
                       seq=seq, vram_used_frac=0.1, ttft_ms=40.0, queue_depth=0,
                       price_usd_hr=0.4, model="gpuha")
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.sendto(f.to_bytes(), ("127.0.0.1", TELEM_PORT)); s.close()


def dig(name="api.gpuha.com"):
    q = struct.pack(">HHHHHH", 0x1234, 0x0100, 1, 0, 0, 0)
    for lbl in name.split("."):
        q += bytes([len(lbl)]) + lbl.encode()
    q += b"\x00" + struct.pack(">HH", 1, 1)
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM); s.settimeout(2)
    s.sendto(q, ("127.0.0.1", DNS_PORT))
    resp, _ = s.recvfrom(4096); s.close()
    ancount = struct.unpack(">H", resp[6:8])[0]
    off = 12
    while resp[off] != 0:
        off += 1 + resp[off]
    off += 1 + 4
    ips = []
    for _ in range(ancount):
        off += 2
        rtype, rclass, ttl, rdlen = struct.unpack(">HHIH", resp[off:off + 10]); off += 10
        rdata = resp[off:off + rdlen]; off += rdlen
        if rtype == 1:
            ips.append(socket.inet_ntoa(rdata))
    return sorted(ips)


def keepalive(node_id, backend, stop_evt, seq_start):
    seq = seq_start
    while not stop_evt.is_set():
        emit(node_id, backend, seq); seq += 1; time.sleep(0.4)


def main():
    logpath = os.path.join(HERE, "tier1_run.log")
    logf = open(logpath, "w")
    proc = subprocess.Popen([sys.executable, os.path.join(HERE, "tier1_dns.py"),
        "--dns-port", str(DNS_PORT), "--telem-port", str(TELEM_PORT), "--fresh", str(FRESH),
        "--pool", "%s=%s" % (GCP_BE, GCP_IP), "--pool", "%s=%s" % (RP_BE, RP_IP)],
        cwd=HERE, stdout=logf, stderr=subprocess.STDOUT)
    time.sleep(1.0)
    results = []

    def check(step, got, want):
        ok = (got == want)
        results.append(ok)
        print("[%s] %-30s got=%s want=%s" % ("PASS" if ok else "FAIL", step, got, want), flush=True)

    try:
        # Step 2 -- baseline: both pools fresh
        for i in range(6):
            emit("gcp1", GCP_BE, 100 + i); emit("rp1", RP_BE, 100 + i); time.sleep(0.2)
        t0 = time.time(); check("2 baseline (both fresh)", dig(), sorted([GCP_IP, RP_IP]))

        # Step 3 -- evacuate RunPod: stop rp1 telemetry (pool dark), keep gcp1 alive
        stop = threading.Event()
        threading.Thread(target=keepalive, args=("gcp1", GCP_BE, stop, 200), daemon=True).start()
        time.sleep(FRESH + 1.0)
        t1 = time.time(); check("3 evacuate RunPod (dark)", dig(), sorted([GCP_IP]))
        print("    -> RunPod evacuated ~%.1fs after going dark" % (FRESH + 1.0))

        # Step 4 -- restore RunPod
        for i in range(6):
            emit("rp1", RP_BE, 300 + i); time.sleep(0.2)
        check("4 restore RunPod", dig(), sorted([GCP_IP, RP_IP]))

        # Step 6 -- failsafe: both pools dark -> full static-safe set + FAILSAFE log
        stop.set(); time.sleep(FRESH + 1.0)
        ips = dig()
        logf.flush(); os.fsync(logf.fileno())
        failsafe_logged = "FAILSAFE" in open(logpath).read()
        check("6 failsafe answer=full set", ips, sorted([GCP_IP, RP_IP]))
        check("6 failsafe logged FAILSAFE", failsafe_logged, True)

        print("\nRESULT:", "ALL PASS" if all(results) else "FAILURES PRESENT", flush=True)
        print("\n---- tier1 transition log ----", flush=True)
        logf.flush()
        for line in open(logpath):
            if "ANSWER SET" in line or "tier1: pools=" in line:
                sys.stdout.write(line)
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=3)
        except Exception:
            proc.kill()


if __name__ == "__main__":
    main()
