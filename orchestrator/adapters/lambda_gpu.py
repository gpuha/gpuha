"""Lambda Cloud GPU-pool adapter (Phase O M2). Cheapest single GPU on Lambda is
gpu_1x_a10 (24GB) -- runs vLLM (Qwen2.5-3B) + router + shim, wired into tier1.

Billing matrix: Lambda => TERMINATE (no stop state; bills until terminated;
terminate destroys local disk). No persistent filesystem is attached.
Lambda resources have no labels, so the run tag is encoded in the instance NAME:
  gpuha-managed--<run_id>--<w>   (reap matches this prefix).
API calls shell out to `curl` -- Lambda's API edge 403s the default python-urllib
User-Agent. Bootstrap + verify use SSH with the plane-held key (~/.ssh/gpuha_lambda).
"""
import json, os, subprocess, tempfile, time
from .base import ProviderAdapter, CapacityError, AdapterError
from ..state import ResourceHandle

API = "https://cloud.lambdalabs.com/api/v1"
NAME_PREFIX = "gpuha-managed--"
KEY_PATH = os.path.expanduser("~/.ssh/gpuha_lambda")
SSH_OPTS = ["-i", KEY_PATH, "-o", "StrictHostKeyChecking=no",
            "-o", "UserKnownHostsFile=/dev/null", "-o", "ConnectTimeout=10",
            "-o", "LogLevel=ERROR"]

BOOTSTRAP_TMPL = r'''#!/bin/bash
exec > /home/ubuntu/gpuha-boot.log 2>&1
set -x
export HF_HUB_ENABLE_HF_TRANSFER=1
PLANE="{plane_ip}"; HTTP="{http_port}"; REGION="{region}"; BACKEND="{backend}"
echo "GPUHA_TS script_start $(date +%s.%N)"
cd /home/ubuntu
python3 -m venv gpuha-venv
. gpuha-venv/bin/activate
pip install -q --upgrade pip
# in-pod parallelism: install the small HF client FIRST so the ~6GB model
# pre-download can overlap the big torch+vllm install below.
pip install -q "huggingface_hub[cli]" hf_transfer
echo "GPUHA_TS hf_ready $(date +%s.%N)"
( echo "GPUHA_TS dl_start $(date +%s.%N)"; python3 -c "from huggingface_hub import snapshot_download; snapshot_download('Qwen/Qwen2.5-3B-Instruct')" > /home/ubuntu/dl.log 2>&1 || echo GPUHA_DL_FAIL; echo "GPUHA_TS dl_done $(date +%s.%N)" ) &
DL_PID=$!
pip install -q "torch==2.8.0+cu128" "torchvision==0.23.0+cu128" "torchaudio==2.8.0+cu128" "vllm" --extra-index-url https://download.pytorch.org/whl/cu128
pip install -q "transformers<5"
echo "GPUHA_TS bigpip_done $(date +%s.%N)"
for f in router.py selection.py telemetry.py vllm_telemetry_shim.py whale.py fake_worker.py; do
  curl -fsS "http://$PLANE:$HTTP/$f" -o "$f"
done
wait $DL_PID
echo "GPUHA_TS dl_joined $(date +%s.%N)"
echo "GPUHA_TS serve_start $(date +%s.%N)"
nohup vllm serve Qwen/Qwen2.5-3B-Instruct --host 0.0.0.0 --port 8000 --served-model-name gpuha > vllm.log 2>&1 &
for i in $(seq 1 180); do curl -sf http://127.0.0.1:8000/v1/models >/dev/null 2>&1 && echo "GPUHA_TS serve_ready $(date +%s.%N)" && break; sleep 3; done
nohup python3 router.py --port 9000 --telemetry-port 5006 --backend gpuha-w1=127.0.0.1:8000 > router.log 2>&1 &
sleep 3
nohup python3 vllm_telemetry_shim.py --worker-url http://127.0.0.1:8000 --node-id gpuha-w1 --dest 127.0.0.1:5006 --dest $PLANE:5106 --backend "$BACKEND" --region "$REGION" --model gpuha > shim.log 2>&1 &
echo GPUHA_BOOT_DONE
'''


BOOTSTRAP_TMPL_2 = r'''#!/bin/bash
exec > /home/ubuntu/gpuha-boot.log 2>&1
set -x
export HF_HUB_ENABLE_HF_TRANSFER=1
PLANE="{plane_ip}"; HTTPP="{http_port}"; REGION="{region}"; BACKEND="{backend}"
cd /home/ubuntu
python3 -m venv gpuha-venv
. gpuha-venv/bin/activate
pip install -q --upgrade pip
pip install -q "torch==2.8.0+cu128" "torchvision==0.23.0+cu128" "torchaudio==2.8.0+cu128" "vllm" --extra-index-url https://download.pytorch.org/whl/cu128
pip install -q "transformers<5" hf_transfer
for f in router.py selection.py telemetry.py vllm_telemetry_shim.py whale.py fake_worker.py; do
  curl -fsS "http://$PLANE:$HTTPP/$f" -o "$f"
done
# TWO vLLM workers co-resident on the one A10 (memory split so both fit in 24GB)
# STAGGERED: start w0, wait until it is serving, THEN w1 (simultaneous start OOMs KV
# cache; memory-split needs each to profile against the other's real allocation).
VA="--served-model-name gpuha --gpu-memory-utilization 0.38 --max-model-len 2048 --max-num-seqs 8 --host 0.0.0.0"
nohup vllm serve Qwen/Qwen2.5-3B-Instruct --port 8000 $VA > vllm0.log 2>&1 &
for i in $(seq 1 120); do curl -sf http://127.0.0.1:8000/v1/models >/dev/null 2>&1 && break; sleep 5; done
nohup vllm serve Qwen/Qwen2.5-3B-Instruct --port 8001 $VA > vllm1.log 2>&1 &
for i in $(seq 1 120); do curl -sf http://127.0.0.1:8001/v1/models >/dev/null 2>&1 && break; sleep 5; done
# ONE router, TWO real-GPU backends
nohup python3 router.py --port 9000 --telemetry-port 5006 --backend gpuha-w1=127.0.0.1:8000 --backend gpuha-w2=127.0.0.1:8001 > router.log 2>&1 &
sleep 3
# one shim per worker (distinct node-id, same pool backend)
nohup python3 vllm_telemetry_shim.py --worker-url http://127.0.0.1:8000 --node-id gpuha-w1 --dest 127.0.0.1:5006 --dest "$PLANE:5106" --backend "$BACKEND" --region "$REGION" --model gpuha > shim0.log 2>&1 &
nohup python3 vllm_telemetry_shim.py --worker-url http://127.0.0.1:8001 --node-id gpuha-w2 --dest 127.0.0.1:5006 --dest "$PLANE:5106" --backend "$BACKEND" --region "$REGION" --model gpuha > shim1.log 2>&1 &
echo GPUHA_BOOT_DONE
'''


# ---- router-head (host-level) templates: A fronts A-local w1 + B's w2 ----
# Instance A: worker A vLLM bound to localhost + router :9000 (public) fronting
# w1=127.0.0.1:8000 and w2=<sib_ip>:8000; telemetry ingest binds 0.0.0.0:5006.
BOOTSTRAP_ROUTER_HEAD = r"""#!/bin/bash
exec > /home/ubuntu/gpuha-boot.log 2>&1
set -x
export HF_HUB_ENABLE_HF_TRANSFER=1
PLANE="{plane_ip}"; HTTP="{http_port}"; REGION="{region}"; BACKEND="{backend}"; SIB="{sib_ip}"
cd /home/ubuntu
python3 -m venv gpuha-venv
. gpuha-venv/bin/activate
pip install -q --upgrade pip
pip install -q "torch==2.8.0+cu128" "torchvision==0.23.0+cu128" "torchaudio==2.8.0+cu128" "vllm" --extra-index-url https://download.pytorch.org/whl/cu128
pip install -q "transformers<5" hf_transfer
for f in router.py selection.py telemetry.py vllm_telemetry_shim.py whale.py fake_worker.py; do
  curl -fsS "http://$PLANE:$HTTP/$f" -o "$f"
done
# worker A vLLM: localhost only (router is co-resident); never publicly exposed
nohup vllm serve Qwen/Qwen2.5-3B-Instruct --host 127.0.0.1 --port 8000 --served-model-name gpuha --gpu-memory-utilization 0.85 --max-model-len 2048 > vllm.log 2>&1 &
for i in $(seq 1 180); do curl -sf http://127.0.0.1:8000/v1/models >/dev/null 2>&1 && break; sleep 5; done
# router-head: two backends across hosts (w1 local, w2 = sibling instance)
nohup python3 router.py --port 9000 --telemetry-port 5006 --backend gpuha-w1=127.0.0.1:8000 --backend gpuha-w2=$SIB:8000 > router.log 2>&1 &
sleep 3
# shim for worker A only (B runs its own shim)
nohup python3 vllm_telemetry_shim.py --worker-url http://127.0.0.1:8000 --node-id gpuha-w1 --dest 127.0.0.1:5006 --dest $PLANE:5106 --backend "$BACKEND" --region "$REGION" --model gpuha > shim.log 2>&1 &
echo GPUHA_BOOT_DONE
"""

# Instance B: worker B vLLM on 0.0.0.0:8000 but iptables-scoped so ONLY the
# router-head (A) may reach :8000; its shim reports liveness to A:5006 + plane.
BOOTSTRAP_WORKER_ONLY = r"""#!/bin/bash
exec > /home/ubuntu/gpuha-boot.log 2>&1
set -x
export HF_HUB_ENABLE_HF_TRANSFER=1
PLANE="{plane_ip}"; HTTP="{http_port}"; REGION="{region}"; BACKEND="{backend}"
HEAD="{head_ip}"; HEADPRIV="{head_priv}"; HEADCONN="{head_conn}"
# firewall: vLLM :8000 reachable ONLY from the router-head (A) pub/priv; else DROP
sudo iptables -A INPUT -i lo -j ACCEPT
sudo iptables -A INPUT -p tcp --dport 8000 -s $HEAD -j ACCEPT
if [ -n "$HEADPRIV" ]; then sudo iptables -A INPUT -p tcp --dport 8000 -s $HEADPRIV -j ACCEPT; fi
sudo iptables -A INPUT -p tcp --dport 8000 -j DROP
cd /home/ubuntu
python3 -m venv gpuha-venv
. gpuha-venv/bin/activate
pip install -q --upgrade pip
pip install -q "torch==2.8.0+cu128" "torchvision==0.23.0+cu128" "torchaudio==2.8.0+cu128" "vllm" --extra-index-url https://download.pytorch.org/whl/cu128
pip install -q "transformers<5" hf_transfer
for f in router.py selection.py telemetry.py vllm_telemetry_shim.py whale.py fake_worker.py; do
  curl -fsS "http://$PLANE:$HTTP/$f" -o "$f"
done
nohup vllm serve Qwen/Qwen2.5-3B-Instruct --host 0.0.0.0 --port 8000 --served-model-name gpuha --gpu-memory-utilization 0.85 --max-model-len 2048 > vllm.log 2>&1 &
for i in $(seq 1 180); do curl -sf http://127.0.0.1:8000/v1/models >/dev/null 2>&1 && break; sleep 5; done
# shim for worker B: liveness to the router-head (A) and the plane
nohup python3 vllm_telemetry_shim.py --worker-url http://127.0.0.1:8000 --node-id gpuha-w2 --dest $HEADCONN:5006 --dest $PLANE:5106 --backend "$BACKEND" --region "$REGION" --model gpuha > shim.log 2>&1 &
echo GPUHA_BOOT_DONE
"""

class LambdaAdapter(ProviderAdapter):
    name = "lambda"

    def __init__(self, api_key=None, plane_ip="203.0.113.10", telem_port=5106,
                 http_port=8090, ssh_key_name="gpuha-plane",
                 instance_type="gpu_1x_a10", regions=None, **kw):
        self.api_key = api_key or os.environ.get("LAMBDA_API_KEY", "")
        self.plane_ip = plane_ip
        self.telem_port = telem_port
        self.http_port = http_port
        self.ssh_key_name = ssh_key_name
        self.instance_type = instance_type
        self.regions = regions or ["us-east-1", "us-west-1"]

    def _api(self, method, path, body=None):
        cmd = ["curl", "-s", "-u", self.api_key + ":", "-X", method, API + path]
        if body is not None:
            cmd += ["-H", "Content-Type: application/json", "-d", json.dumps(body)]
        cmd += ["-w", "\n__HTTP__%{http_code}"]
        out = subprocess.run(cmd, capture_output=True, text=True).stdout
        raw, _, code = out.rpartition("__HTTP__")
        code = code.strip()
        try:
            data = json.loads(raw) if raw.strip() else {}
        except json.JSONDecodeError:
            data = {"raw": raw}
        if code not in ("200", "201"):
            msg = json.dumps(data)
            low = msg.lower()
            if "capacity" in low or "insufficient" in low or "not enough" in low:
                raise CapacityError(msg)
            raise AdapterError("lambda API %s %s -> %s: %s" % (method, path, code, msg))
        return data.get("data", data)

    def _ssh(self, ip, remote_cmd, timeout=60):
        return subprocess.run(["ssh"] + SSH_OPTS + ["ubuntu@" + ip, remote_cmd],
                              capture_output=True, text=True, timeout=timeout)

    def _wait_ssh(self, ip, tries=40, interval=5):
        for _ in range(tries):
            try:
                r = self._ssh(ip, "echo ok", timeout=15)
                if r.returncode == 0 and "ok" in r.stdout:
                    return True
            except subprocess.TimeoutExpired:
                pass
            time.sleep(interval)
        return False

    def acquire(self, pool_spec, run_id):
        extra = pool_spec.extra or {}
        itype = pool_spec.machine_type or self.instance_type
        gw = int(extra.get("gpu_workers", 1))
        router_head = str(extra.get("router_head", "")).lower() in ("1", "true", "yes")
        regions = extra.get("regions") or (
            [pool_spec.region] if pool_spec.region else self.regions)
        base = NAME_PREFIX + run_id
        last = None
        for region in regions:
            try:
                aid, apub, apriv = self._launch(region, itype, base + "--0")
                sib = None
                if router_head:
                    try:
                        bid, bpub, bpriv = self._launch(region, itype, base + "--1")
                    except Exception:
                        self._terminate([aid])
                        raise
                    sib = {"id": bid, "ip": bpub, "private_ip": bpriv,
                           "node": "gpuha-w2", "name": base + "--1"}
                hx = {"instance_type": itype, "name": base + "--0",
                      "gpu_workers": gw, "private_ip": apriv}
                if sib:
                    hx["router_head"] = True
                    hx["sibling"] = sib
                return [ResourceHandle(
                    provider="lambda", kind="instance", id=aid, pool=pool_spec.name,
                    role=pool_spec.role, region=region, public_ip=apub,
                    tag=base + "--0", extra=hx)]
            except CapacityError as e:
                last = e
                print("[lambda] capacity-out in %s (%s); trying next region"
                      % (region, itype))
                continue
        raise CapacityError("no capacity for %s in %s (last: %s)"
                            % (itype, regions, last))

    def _launch(self, region, itype, name):
        res = self._api("POST", "/instance-operations/launch", {
            "region_name": region, "instance_type_name": itype,
            "ssh_key_names": [self.ssh_key_name], "name": name, "quantity": 1})
        iid = res["instance_ids"][0]
        self._wait_active(iid)
        d = self._api("GET", "/instances/" + iid)
        return iid, d.get("ip"), (d.get("private_ip") or "")

    def _wait_active(self, iid, tries=60, interval=10):
        for _ in range(tries):
            d = self._api("GET", "/instances/" + iid)
            if d.get("status") == "active" and d.get("ip"):
                return d["ip"]
            time.sleep(interval)
        raise AdapterError("instance %s never became active" % iid)

    def bootstrap(self, handle, role):
        ip = handle.public_ip
        ex = handle.extra or {}
        if ex.get("router_head") and ex.get("sibling"):
            sib = ex["sibling"]
            a_pub = ip
            a_priv = ex.get("private_ip") or ""
            b_pub = sib.get("ip")
            b_priv = sib.get("private_ip") or ""
            sib_conn = b_priv or b_pub          # A router -> B worker :8000
            head_conn = a_priv or a_pub          # B shim -> A telemetry :5006
            # worker B first (firewall-scoped), then the router-head on A
            self._push_boot(b_pub, BOOTSTRAP_WORKER_ONLY.format(
                plane_ip=self.plane_ip, http_port=self.http_port,
                region=handle.region, backend=handle.pool,
                head_ip=a_pub, head_priv=a_priv, head_conn=head_conn))
            self._push_boot(a_pub, BOOTSTRAP_ROUTER_HEAD.format(
                plane_ip=self.plane_ip, http_port=self.http_port,
                region=handle.region, backend=handle.pool, sib_ip=sib_conn))
            return
        tmpl = BOOTSTRAP_TMPL_2 if int(ex.get("gpu_workers", 1)) >= 2 else BOOTSTRAP_TMPL
        script = tmpl.format(plane_ip=self.plane_ip, http_port=self.http_port,
                             region=handle.region, backend=handle.pool)
        self._push_boot(ip, script)

    def _push_boot(self, ip, script):
        if not self._wait_ssh(ip):
            raise AdapterError("SSH to %s never came up" % ip)
        fd, sfile = tempfile.mkstemp(prefix="gpuha-lam-boot-", suffix=".sh")
        with os.fdopen(fd, "w") as f:
            f.write(script)
        try:
            scp = ["scp"] + SSH_OPTS + [sfile,
                   "ubuntu@%s:/home/ubuntu/gpuha_boot.sh" % ip]
            r = subprocess.run(scp, capture_output=True, text=True, timeout=60)
            if r.returncode != 0:
                raise AdapterError("scp bootstrap failed: %s" % r.stderr)
            try:
                self._ssh(ip, "chmod +x /home/ubuntu/gpuha_boot.sh && "
                          "setsid /home/ubuntu/gpuha_boot.sh </dev/null "
                          ">/dev/null 2>&1 &", timeout=20)
            except subprocess.TimeoutExpired:
                pass  # SSH channel did not close; setsid job is detached & running
        finally:
            os.remove(sfile)

    def verify(self, handle):
        d = self._api("GET", "/instances/" + handle.id)
        return d.get("status") == "active"

    def completion(self, handle):
        ip = handle.public_ip
        body = ('{"model":"gpuha","messages":[{"role":"user",'
                '"content":"Reply with exactly: GPU HA online"}],"max_tokens":16}')
        cmd = ("curl -s -D /tmp/h -o /tmp/b -w 'HTTP=%%{http_code}' "
               "http://127.0.0.1:9000/v1/chat/completions "
               "-H 'Content-Type: application/json' -d '%s' ; echo ; "
               "grep -i 'x-gpuha' /tmp/h ; echo '---BODY---' ; head -c 500 /tmp/b" % body)
        return self._ssh(ip, cmd, timeout=45).stdout

    def _terminate(self, ids, tries=8, interval=15):
        """Lambda 500s terminate while an instance is booting/terminating. Retry,
        and treat 'already gone' as success."""
        ids = list(ids)
        for _ in range(tries):
            try:
                self._api("POST", "/instance-operations/terminate", {"instance_ids": ids})
                return
            except AdapterError as e:
                low = str(e).lower()
                if "500" in low or "internal" in low or "capacity" in low:
                    time.sleep(interval); continue
                if "not found" in low or "does not exist" in low:
                    return
                time.sleep(interval)
            remaining = {i["id"] for i in (self._api("GET", "/instances") or [])}
            if not (set(ids) & remaining):
                return
        remaining = {i["id"] for i in (self._api("GET", "/instances") or [])}
        if set(ids) & remaining:
            raise AdapterError("terminate failed after retries for %s" % ids)

    def diag(self, handle):
        cmd = ("echo === router.log ===; tail -25 /home/ubuntu/router.log 2>&1; "
               "echo === shim.log ===; tail -6 /home/ubuntu/shim.log 2>&1; "
               "echo === vllm.log tail ===; tail -4 /home/ubuntu/vllm.log 2>&1; "
               "echo === procs ===; pgrep -a -f 'vllm serve|router.py|shim' 2>&1")
        try:
            return self._ssh(handle.public_ip, cmd, timeout=30).stdout
        except Exception as e:
            return "diag ssh failed: %s" % e

    def teardown(self, handle):
        ids = [handle.id]
        sib = (handle.extra or {}).get("sibling")
        if sib and sib.get("id"):
            ids.append(sib["id"])
        self._terminate(ids)

    def reap(self, run_id):
        insts = self._api("GET", "/instances")
        ids, killed = [], []
        for i in (insts or []):
            nm = i.get("name") or ""
            if not nm.startswith(NAME_PREFIX):
                continue
            if run_id is None or nm.startswith(NAME_PREFIX + run_id):
                ids.append(i["id"]); killed.append(nm)
        if ids:
            self._terminate(ids)
        return killed
