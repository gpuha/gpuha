"""RunPod GPU adapter (CLOSED). Mirrors ProviderAdapter.
Scars encoded (docs/RUNPOD.md + full-run brief):
 - DELETE pods, never stop (stopped pods can be unrestartable).
 - CUDA 12.x driver pin: torch==2.8.0+cu128 -> vllm 0.11 + transformers<5 + hf_transfer.
   allowedCudaVersions forces a host whose driver supports our stack.
 - name-prefix tag gpuha-managed--<run-id>--N (RunPod has no label API).
 - NAT wrinkle: pod egress IP (curl ifconfig.me from inside) != direct-TCP proxy publicIp.
   Firewall udp/5106 scopes to EGRESS ip; router reachable at proxy publicIp:mappedPort.
   Pool map advertises publicIp only (DNS A carries no port -> documented demo caveat).
 - router/shim on 8011+ to dodge pod nginx/jupyter collisions.
 - curl for REST (urllib UA gets edge-blocked); setsid not nohup; PYTHONUNBUFFERED=1.
 - fetch list includes fake_worker.py (M2 lesson: router imports it).
"""
import os, json, time, base64, subprocess, secrets
from ..state import ResourceHandle
from .base import ProviderAdapter, CapacityError, AdapterError

REST = "https://rest.runpod.io/v1"
NAME_PREFIX = "gpuha-managed--"
IMAGE = "runpod/pytorch:2.8.0-py3.11-cuda12.8.1-cudnn-devel-ubuntu22.04"
GPU_L4 = "NVIDIA L4"
VLLM_PORT = 8000
ROUTER_PORT = 8011
SSH_OPTS = ["-o","StrictHostKeyChecking=no","-o","UserKnownHostsFile=/dev/null",
            "-o","ConnectTimeout=10","-o","LogLevel=ERROR"]

BOOTSTRAP_TMPL = r'''#!/bin/bash
set -x
export HF_HUB_ENABLE_HF_TRANSFER=1
export PYTHONUNBUFFERED=1
mkdir -p /workspace && cd /workspace
for f in router.py vllm_telemetry_shim.py fake_worker.py selection.py telemetry.py whale.py; do
  for i in 1 2 3 4 5 6; do curl -fsS -o "$f" "http://__PLANE__:8090/$f" && break; sleep 3; done
done
cd /workspace && python3 -c "import router, vllm_telemetry_shim, selection, telemetry, whale, fake_worker" || { echo GPUHA_BOOTSTRAP_IMPORT_FAIL; exit 3; }
pip install -q "torch==2.8.0+cu128" "torchvision==0.23.0+cu128" "torchaudio==2.8.0+cu128" "vllm" --extra-index-url https://download.pytorch.org/whl/cu128 >> /workspace/pip.log 2>&1
pip install -q "transformers<5" hf_transfer >> /workspace/pip.log 2>&1
python3 -c "import torch,vllm;print('PINCHECK',vllm.__version__,torch.__version__,torch.cuda.is_available())" >> /workspace/pip.log 2>&1
setsid bash -c 'vllm serve Qwen/Qwen2.5-3B-Instruct --host 0.0.0.0 --port __VLLM__ --served-model-name gpuha --gpu-memory-utilization 0.85 --max-model-len 4096 > /workspace/vllm.log 2>&1' &
for i in $(seq 1 180); do curl -sf http://127.0.0.1:__VLLM__/v1/models >/dev/null && break; sleep 5; done
setsid bash -c 'python3 router.py --port __ROUTER__ --telemetry-port 5006 --backend __NODE__=127.0.0.1:__VLLM__ > /workspace/router.log 2>&1' &
sleep 3
setsid bash -c 'python3 vllm_telemetry_shim.py --worker-url http://127.0.0.1:__VLLM__ --node-id __NODE__ --dest 127.0.0.1:5006 --dest __PLANE__:5106 --backend __POOL__ --region __REGION__ --model gpuha > /workspace/shim.log 2>&1' &
echo BOOTSTRAP_KICKED
'''

class RunPodAdapter(ProviderAdapter):
    def __init__(self, api_key=None, plane_ip="203.0.113.10", telem_port=5106,
                 ssh_key="/root/.ssh/gpuha_lambda", pubkey="/root/.ssh/gpuha_lambda.pub"):
        self.api_key = api_key or os.environ.get("RUNPOD_API_KEY","")
        self.plane_ip = plane_ip
        self.telem_port = telem_port
        self.ssh_key = ssh_key
        self.pubkey_path = pubkey

    @staticmethod
    def tag_for(run_id):
        return NAME_PREFIX + run_id

    # ---- REST via curl ----
    def _curl(self, method, path, body=None):
        cmd = ["curl","-s","-X",method, REST+path,
               "-H","Authorization: Bearer "+self.api_key,
               "-H","Content-Type: application/json"]
        if body is not None:
            cmd += ["-d", json.dumps(body)]
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=90).stdout
        try:
            return json.loads(out) if out.strip() else {}
        except Exception:
            return {"_raw": out}

    def _ssh(self, ip, port, cmd, timeout=60):
        full = ["ssh","-i",self.ssh_key,"-p",str(port)] + SSH_OPTS + ["root@"+ip, cmd]
        return subprocess.run(full, capture_output=True, text=True, timeout=timeout).stdout

    def _wait_ssh(self, ip, port, tries=40, interval=6):
        for _ in range(tries):
            r = subprocess.run(["ssh","-i",self.ssh_key,"-p",str(port)] + SSH_OPTS +
                               ["root@"+ip,"echo ok"], capture_output=True, text=True)
            if "ok" in r.stdout:
                return True
            time.sleep(interval)
        return False

    def _ensure_fileserver(self):
        # idempotent: serve repo root on :8090 so pods can fetch router/shim/fake_worker
        r = subprocess.run(["bash","-c","curl -sf http://127.0.0.1:8090/router.py >/dev/null && echo up || echo down"],
                           capture_output=True, text=True).stdout
        if "up" not in r:
            repo = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
            subprocess.run(["bash","-c",
                "cd %s && setsid python3 -m http.server 8090 >/tmp/fileserver.log 2>&1 &" % repo])
            time.sleep(2)

    def _pool_n(self, pool_spec):
        return int(getattr(pool_spec,"workers",None) or (getattr(pool_spec,"extra",{}) or {}).get("gpu_workers",1) or 1)

    def _create_pod(self, run_id, i, pubkey, ports):
        name = NAME_PREFIX + run_id + "--%d" % i
        body = {"name":name, "imageName":IMAGE, "gpuTypeIds":[GPU_L4], "gpuCount":1,
                "cloudType":"SECURE", "computeType":"GPU",
                "containerDiskInGb":25, "volumeInGb":0,
                "ports":["22/tcp"] + ["%d/tcp" % p for p in ports],
                "allowedCudaVersions":["12.4","12.5","12.6","12.7","12.8"],
                "env":{"PUBLIC_KEY":pubkey}}
        r = self._curl("POST","/pods", body)
        pid = r.get("id") or (r.get("data") or {}).get("id") if isinstance(r,dict) else None
        if not pid:
            msg = json.dumps(r)[:400]
            if any(k in msg.lower() for k in ("no instances","unavailable","capacity","no longer any")):
                raise CapacityError("runpod L4 capacity-out: "+msg)
            raise AdapterError("runpod create failed: "+msg)
        return name, pid

    def _mk_handle(self, pid, name, i, pool_spec, run_id, extra_extra=None):
        ex = {"name":name,"worker_index":i,"pool":pool_spec.backend,"region":"runpod"}
        if extra_extra:
            ex.update(extra_extra)
        return ResourceHandle(provider="runpod", kind="pod", id=pid,
            pool=pool_spec.backend, role=getattr(pool_spec,"role","gpu"),
            region="runpod", public_ip="", tag=self.tag_for(run_id), extra=ex)

    def acquire(self, pool_spec, run_id):
        self._ensure_fileserver()
        pubkey = open(self.pubkey_path).read().strip()
        ex = pool_spec.extra or {}
        rh = str(ex.get("router_head","")).lower() in ("1","true","yes")
        wo = str(ex.get("worker_only","")).lower() in ("1","true","yes")
        if rh:
            secret = secrets.token_hex(16)
            an, aid = self._create_pod(run_id, 0, pubkey, [ROUTER_PORT])
            bn, bid = self._create_pod(run_id, 1, pubkey, [VLLM_PORT])
            aip, apm = self._wait_running(aid, want_port=ROUTER_PORT)
            bip, bpm = self._wait_running(bid, want_port=VLLM_PORT)
            H = self._mk_handle(aid, an, 0, pool_spec, run_id, {
                "router_head":True, "secret":secret,
                "router_pub_port":apm.get(str(ROUTER_PORT)), "ssh_port":apm.get("22"),
                "sibling":{"id":bid,"name":bn,"pub":bip,
                           "vllm_pub_port":bpm.get(str(VLLM_PORT)),"ssh_port":bpm.get("22"),
                           "node":pool_spec.backend+"-w2"}})
            H.public_ip = aip
            return [H]
        if wo:
            secret = secrets.token_hex(16)
            bn, bid = self._create_pod(run_id, 0, pubkey, [VLLM_PORT])
            bip, bpm = self._wait_running(bid, want_port=VLLM_PORT)
            H = self._mk_handle(bid, bn, 0, pool_spec, run_id, {
                "worker_only":True, "secret":secret,
                "vllm_pub_port":bpm.get(str(VLLM_PORT)), "ssh_port":bpm.get("22")})
            H.public_ip = bip
            return [H]
        n = self._pool_n(pool_spec)
        handles = []
        for i in range(n):
            name, pid = self._create_pod(run_id, i, pubkey, [ROUTER_PORT])
            handles.append(self._mk_handle(pid, name, i, pool_spec, run_id))
        for h in handles:
            ip, ports = self._wait_running(h.id)
            h.public_ip = ip
            h.extra["router_pub_port"] = ports.get(str(ROUTER_PORT))
            h.extra["ssh_port"] = ports.get("22")
        return handles

    def _wait_running(self, pid, want_port=ROUTER_PORT, tries=90, interval=10):
        last = None
        for _ in range(tries):
            r = self._curl("GET","/pods/"+pid)
            last = r
            status = (r.get("desiredStatus") or r.get("status") or "") if isinstance(r,dict) else ""
            pubip = (r.get("publicIp") or "") if isinstance(r,dict) else ""
            pm = {}
            raw_pm = r.get("portMappings") if isinstance(r,dict) else None
            if isinstance(raw_pm, dict):
                pm = {str(k):v for k,v in raw_pm.items()}
            if status == "RUNNING" and pubip and pm.get(str(want_port)) and pm.get("22"):
                return pubip, pm
            time.sleep(interval)
        raise AdapterError("runpod pod %s not RUNNING/mapped in time; last=%s" % (pid, json.dumps(last)[:300]))

    def _boot_pod(self, ip, sp, script, register_egress=True):
        if not self._wait_ssh(ip, sp):
            raise AdapterError("runpod ssh never came up for %s:%s" % (ip, sp))
        if register_egress:
            egress = self._ssh(ip, sp, "curl -s ifconfig.me").strip()
            if egress:
                subprocess.run(["bash","-c","ufw allow from %s to any port 5106 proto udp" % egress],
                               capture_output=True, text=True)
        b64 = base64.b64encode(script.encode()).decode()
        push = "mkdir -p /workspace && echo %s | base64 -d > /workspace/bootstrap.sh && setsid bash /workspace/bootstrap.sh >/workspace/boot.log 2>&1 & echo KICKED" % b64
        try:
            self._ssh(ip, sp, push, timeout=60)
        except subprocess.TimeoutExpired:
            pass

    def bootstrap(self, handle, role):
        ex = handle.extra or {}
        if ex.get("router_head") and ex.get("sibling"):
            sib = ex["sibling"]; secret = ex["secret"]
            w2conn = "%s:%s" % (sib["pub"], sib["vllm_pub_port"])
            nodeA = handle.pool + "-w1"; nodeB = sib["node"]
            handle.extra["node_id"] = nodeA
            wscript = BOOTSTRAP_RP_WORKER.replace("__VLLM__",str(VLLM_PORT)).replace("__SECRET__",secret)
            self._boot_pod(sib["pub"], sib["ssh_port"], wscript, register_egress=False)
            ascript = (BOOTSTRAP_RP_HEAD.replace("__PLANE__",self.plane_ip)
                       .replace("__VLLM__",str(VLLM_PORT)).replace("__ROUTER__",str(ROUTER_PORT))
                       .replace("__NODEA__",nodeA).replace("__NODEB__",nodeB)
                       .replace("__W2CONN__",w2conn).replace("__SECRET__",secret)
                       .replace("__POOL__",handle.pool).replace("__REGION__",ex.get("region","runpod")))
            self._boot_pod(handle.public_ip, ex.get("ssh_port"), ascript, register_egress=True)
            return
        if ex.get("worker_only"):
            secret = ex["secret"]
            handle.extra["node_id"] = handle.pool + "-w1"
            wscript = BOOTSTRAP_RP_WORKER.replace("__VLLM__",str(VLLM_PORT)).replace("__SECRET__",secret)
            self._boot_pod(handle.public_ip, ex.get("ssh_port"), wscript, register_egress=False)
            return
        ip = handle.public_ip
        sp = ex.get("ssh_port")
        node = handle.pool + "-w%d" % (ex.get("worker_index",0)+1)
        handle.extra["node_id"] = node
        script = (BOOTSTRAP_TMPL.replace("__PLANE__",self.plane_ip)
                  .replace("__VLLM__",str(VLLM_PORT)).replace("__ROUTER__",str(ROUTER_PORT))
                  .replace("__NODE__",node).replace("__POOL__",handle.pool)
                  .replace("__REGION__",ex.get("region","runpod")))
        self._boot_pod(ip, sp, script, register_egress=True)

    def verify(self, handle, tries=60, interval=15):
        ex = handle.extra or {}
        ip = handle.public_ip
        if ex.get("worker_only"):
            vp = ex.get("vllm_pub_port"); secret = ex.get("secret","")
            for _ in range(tries):
                code = subprocess.run(["bash","-c",
                    "curl -s -m 8 -o /dev/null -w '%%{http_code}' -H 'Authorization: Bearer %s' http://%s:%s/v1/models"
                    % (secret, ip, vp)], capture_output=True, text=True).stdout.strip()
                if code == "200":
                    return True
                time.sleep(interval)
            return False
        rp = ex.get("router_pub_port")
        for _ in range(tries):
            out = subprocess.run(["bash","-c",
                "curl -s -m 8 http://%s:%s/__stats" % (ip, rp)], capture_output=True, text=True).stdout
            if out and "eligible" in out and handle.extra.get("node_id","") in out:
                return True
            time.sleep(interval)
        return False

    def completion(self, handle, prompt="Say 'GPU HA online' in four words."):
        ip = handle.public_ip; rp = handle.extra.get("router_pub_port")
        body = json.dumps({"model":"gpuha","messages":[{"role":"user","content":prompt}],"max_tokens":16})
        out = subprocess.run(["bash","-c",
            "curl -s -m 30 -D - -o /tmp/rp_body http://%s:%s/v1/chat/completions -H 'Content-Type: application/json' -d %s"
            % (ip, rp, json.dumps(body))], capture_output=True, text=True).stdout
        return out

    def diag(self, handle):
        ip = handle.public_ip; sp = handle.extra.get("ssh_port")
        try:
            return self._ssh(ip, sp, "tail -5 /workspace/vllm.log /workspace/router.log /workspace/shim.log /workspace/pip.log 2>&1", timeout=30)
        except Exception as e:
            return "diag ssh failed: %s" % e

    def _delete(self, pid, tries=5, interval=8):
        for _ in range(tries):
            self._curl("DELETE","/pods/"+pid)
            r = self._curl("GET","/pods/"+pid)
            gone = (not isinstance(r,dict)) or (r.get("id") != pid) or bool(r.get("error"))
            if gone:
                return True
            time.sleep(interval)
        return False

    def teardown(self, handle):
        ex = handle.extra or {}
        self._delete(handle.id)
        sib = ex.get("sibling")
        if sib and sib.get("id"):
            self._delete(sib["id"])

    def reap(self, run_id=None):
        r = self._curl("GET","/pods")
        d = r.get("data", r) if isinstance(r, dict) else r
        killed = []
        for p in (d or []):
            nm = (p.get("name") or "") if isinstance(p,dict) else ""
            if not nm.startswith(NAME_PREFIX):
                continue
            if run_id is not None and not nm.startswith(NAME_PREFIX+run_id):
                continue
            if self._delete(p["id"]):
                killed.append(nm)
        return killed


# ---- router-head (host-level, 2-pod) templates ----------------------------
# Pod B (worker-only): vLLM on 0.0.0.0 with --api-key; :8000 is public via the
# RunPod TCP proxy, so the api-key IS the firewall (negative test = the gate).
BOOTSTRAP_RP_WORKER = r'''#!/bin/bash
set -x
export HF_HUB_ENABLE_HF_TRANSFER=1
export PYTHONUNBUFFERED=1
mkdir -p /workspace && cd /workspace
pip install -q "torch==2.8.0+cu128" "torchvision==0.23.0+cu128" "torchaudio==2.8.0+cu128" "vllm" --extra-index-url https://download.pytorch.org/whl/cu128
pip install -q "transformers<5" hf_transfer >> /workspace/pip.log 2>&1
python3 -c "import torch,vllm;print('PINCHECK',vllm.__version__,torch.cuda.is_available())" >> /workspace/pip.log 2>&1
setsid bash -c 'vllm serve Qwen/Qwen2.5-3B-Instruct --host 0.0.0.0 --port __VLLM__ --served-model-name gpuha --api-key __SECRET__ --gpu-memory-utilization 0.85 --max-model-len 4096 > /workspace/vllm.log 2>&1' &
echo BOOTSTRAP_KICKED
'''

# Pod A (router-head): w1 vLLM localhost-only (no key, not exposed) + router
# fronting w1 local and w2 = B via proxy (with --backend-auth). Liveness for w2
# is RELOCATED here: A runs w2's shim scraping B's proxy addr and emitting normal
# frames to A's local router -> B dies, scrape stops, silence=death evicts w2.
BOOTSTRAP_RP_HEAD = r'''#!/bin/bash
set -x
export HF_HUB_ENABLE_HF_TRANSFER=1
export PYTHONUNBUFFERED=1
mkdir -p /workspace && cd /workspace
for f in router.py vllm_telemetry_shim.py selection.py telemetry.py whale.py fake_worker.py; do
  for i in 1 2 3 4 5 6; do curl -fsS -o "$f" "http://__PLANE__:8090/$f" && break; sleep 3; done
done
python3 -c "import router, vllm_telemetry_shim, selection, telemetry, whale, fake_worker" || { echo GPUHA_BOOTSTRAP_IMPORT_FAIL; exit 3; }
pip install -q "torch==2.8.0+cu128" "torchvision==0.23.0+cu128" "torchaudio==2.8.0+cu128" "vllm" --extra-index-url https://download.pytorch.org/whl/cu128
pip install -q "transformers<5" hf_transfer >> /workspace/pip.log 2>&1
python3 -c "import torch,vllm;print('PINCHECK',vllm.__version__,torch.cuda.is_available())" >> /workspace/pip.log 2>&1
setsid bash -c 'vllm serve Qwen/Qwen2.5-3B-Instruct --host 127.0.0.1 --port __VLLM__ --served-model-name gpuha --gpu-memory-utilization 0.85 --max-model-len 4096 > /workspace/vllm.log 2>&1' &
for i in $(seq 1 180); do curl -sf http://127.0.0.1:__VLLM__/v1/models >/dev/null 2>&1 && break; sleep 5; done
setsid bash -c 'python3 router.py --port __ROUTER__ --telemetry-port 5006 --backend __NODEA__=127.0.0.1:__VLLM__ --backend __NODEB__=__W2CONN__ --backend-auth __NODEB__=__SECRET__ > /workspace/router.log 2>&1' &
sleep 3
setsid bash -c 'python3 vllm_telemetry_shim.py --worker-url http://127.0.0.1:__VLLM__ --node-id __NODEA__ --dest 127.0.0.1:5006 --dest __PLANE__:5106 --backend __POOL__ --region __REGION__ --model gpuha > /workspace/shimA.log 2>&1' &
setsid bash -c 'python3 vllm_telemetry_shim.py --worker-url http://__W2CONN__ --node-id __NODEB__ --dest 127.0.0.1:5006 --dest __PLANE__:5106 --backend __POOL__ --region __REGION__ --model gpuha > /workspace/shimB.log 2>&1' &
echo BOOTSTRAP_KICKED
'''
