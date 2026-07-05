#!/usr/bin/env python3
"""Idempotent patch: add --pool-file (+ SIGHUP reload) to tier1_dns.py so the
orchestrator can publish dynamic pools without editing the systemd ExecStart.
Static --pool args (the always-on plane) are preserved and merged with the file."""
import re, sys

PATH = sys.argv[1] if len(sys.argv) > 1 else "tier1_dns.py"
src = open(PATH).read()
orig = src

if "import os, signal" not in src and "\nimport signal" not in src:
    src = src.replace("import socket, struct, threading, time, argparse",
                      "import socket, struct, threading, time, argparse\nimport os, signal  # --pool-file + SIGHUP reload",
                      1)

helper = '''
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

'''
if "_load_pool_file" not in src:
    anchor = "from telemetry import TelemetryIngest"
    idx = src.index(anchor)
    eol = src.index("\n", idx) + 1
    src = src[:eol] + helper + src[eol:]

if "--pool-file" not in src:
    src = re.sub(
        r'(ap\.add_argument\("--failsafe-ip".*?\)\n)',
        r'\1    ap.add_argument("--pool-file", default=None,\n'
        r'                    help="file of backend=ip lines (dynamic pools; merged with --pool, re-read on SIGHUP)")\n',
        src, count=1, flags=re.DOTALL)

if "pool_map.update(_load_pool_file" not in src:
    src = src.replace(
        "    if not pool_map:\n",
        "    pool_map.update(_load_pool_file(a.pool_file))\n    if not pool_map:\n",
        1)

if "def _reload" not in src:
    reload_block = '''
    def _reload(_sig, _frm):
        merged = {}
        for _p in a.pool:
            _k, _, _v = _p.partition("="); merged[_k] = _v
        merged.update(_load_pool_file(a.pool_file))
        t.pool_map = merged
        log("SIGHUP reload -> pools=%s" % merged)
    signal.signal(signal.SIGHUP, _reload)
'''
    src = src.replace(
        'log("pools=%s fresh=%.1fs" % (pool_map, a.fresh))\n',
        'log("pools=%s fresh=%.1fs" % (pool_map, a.fresh))\n' + reload_block,
        1)

if src == orig:
    print("no changes (already patched?)")
else:
    open(PATH, "w").write(src)
    print("patched %s" % PATH)
