"""Tier-1 wiring: publish the dynamic pool->IP map the plane's tier1_dns.py
reads via --pool-file, then make it take effect. Dumb-reliable = restart the
service (hot SIGHUP reload is a later refinement). Firewall scoping to each
instance /32 is a HUMAN GATE (Scott / Linode) and is only recorded here.
"""
import os, subprocess


class Wiring:
    def add_pool(self, name, ip): raise NotImplementedError
    def remove_pool(self, name): raise NotImplementedError
    def apply(self): raise NotImplementedError
    def firewall_note(self, ip): raise NotImplementedError


class FakeWiring(Wiring):
    """Offline: writes a pool-map file, records apply()/firewall calls, no systemctl."""
    def __init__(self, pool_map_file):
        self.pool_map_file = pool_map_file
        self.pools = {}
        self.applies = 0
        self.firewall_calls = []

    def add_pool(self, name, ip): self.pools[name] = ip
    def remove_pool(self, name): self.pools.pop(name, None)

    def apply(self):
        os.makedirs(os.path.dirname(self.pool_map_file) or ".", exist_ok=True)
        with open(self.pool_map_file, "w") as f:
            for n, ip in sorted(self.pools.items()):
                f.write("%s=%s\n" % (n, ip))
        self.applies += 1

    def firewall_note(self, ip):
        self.firewall_calls.append(ip)


class RealWiring(Wiring):
    """On the plane: write --pool-file and `systemctl restart <service>`."""
    def __init__(self, pool_map_file, service="gpuha-tier1"):
        self.pool_map_file = os.path.expanduser(pool_map_file)
        self.service = service
        self.pools = self._read()

    def _read(self):
        pools = {}
        if os.path.exists(self.pool_map_file):
            for line in open(self.pool_map_file):
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    k, _, v = line.partition("=")
                    pools[k.strip()] = v.strip()
        return pools

    def add_pool(self, name, ip): self.pools[name] = ip
    def remove_pool(self, name): self.pools.pop(name, None)

    def apply(self):
        os.makedirs(os.path.dirname(self.pool_map_file) or ".", exist_ok=True)
        tmp = self.pool_map_file + ".tmp"
        with open(tmp, "w") as f:
            f.write("# dynamic orchestrated pools (managed by gpuha) - do not edit by hand\n")
            for n, ip in sorted(self.pools.items()):
                f.write("%s=%s\n" % (n, ip))
        os.replace(tmp, self.pool_map_file)
        subprocess.run(["systemctl", "restart", self.service], check=True)

    def firewall_note(self, ip):
        print("[FIREWALL GATE] scope plane inbound udp/5106 <- %s/32 (Scott)" % ip)
