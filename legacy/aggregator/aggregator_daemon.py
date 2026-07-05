#!/usr/bin/env python3
import socket
import json
import threading
import time
import sys

# Inbound Configuration
UDP_LISTEN_IP = "0.0.0.0"
UDP_LISTEN_PORT = 5005

# Outbound CoreDNS Edge Broadcast Configuration
COREDNS_EDGE_IPS = ["74.207.243.66"]
COREDNS_BROADCAST_PORT = 5006

cluster_state = {}
state_lock = threading.Lock()

def listen_telemetry():
    """Listens for high-frequency incoming telemetry packets from target nodes."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.bind((UDP_LISTEN_IP, UDP_LISTEN_PORT))
    print(f"[INIT] Main thread bound to UDP port {UDP_LISTEN_PORT}. Awaiting target packets...", flush=True)

    while True:
        try:
            data, addr = sock.recvfrom(1024)
            payload = json.loads(data.decode('utf-8'))
            target_id = payload.get("target_id")

            if target_id:
                with state_lock:
                    cluster_state[target_id] = {
                        "ttft_ms": payload.get("ttft_ms"),
                        "vram_saturation_pct": payload.get("vram_saturation_pct"),
                        "spot_price_hr": payload.get("spot_price_hr"),
                        "last_seen_epoch": payload.get("timestamp"),
                        "source_ip": addr[0]
                    }
                    # Optional: Uncomment the next line if you want to see every raw inbound packet in journalctl
                    # print(f"[INBOUND] Received packet from {target_id}", flush=True)
        except Exception as e:
            print(f"[WARN] Error processing inbound packet: {e}", flush=True)

def broadcast_state_to_edge():
    """Asynchronous background loop: Serializes and blasts the global state map to edge nodes."""
    print(f"[INIT] Edge Broadcast Thread Initiated on Port {COREDNS_BROADCAST_PORT}...", flush=True)
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)

    while True:
        try:
            time.sleep(1.0)  # Broadcast interval gating

            with state_lock:
                if not cluster_state:
                    # Log that we are waiting for target profiles before broadcasting
                    print("[EDGE SYNC] State map empty. Awaiting target node check-ins...", flush=True)
                    continue
                state_snapshot = json.dumps(cluster_state).encode('utf-8')

                for edge_ip in COREDNS_EDGE_IPS:
                    sock.sendto(state_snapshot, (edge_ip, COREDNS_BROADCAST_PORT))

            print(f"[EDGE SYNC] Broadcasted state snapshot ({len(state_snapshot)} bytes) out-of-band.", flush=True)

        except Exception as e:
            print(f"[ERROR] Edge Broadcast loop failure: {e}", flush=True)

if __name__ == "__main__":
    print("==================================================================", flush=True)
    print("     GPUHA CENTRAL TELEMETRY AGGREGATOR ENGINE v1.2   ", flush=True)
    print("==================================================================", flush=True)

    try:
        broadcast_thread = threading.Thread(target=broadcast_state_to_edge, daemon=True)
        broadcast_thread.start()

        listen_telemetry()

    except KeyboardInterrupt:
        print("\nAggregator shutting down cleanly.", flush=True)
        sys.exit(0)
