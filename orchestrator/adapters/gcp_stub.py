"""GCP CPU stub-pool adapter. Creates an e2-micro, starts the Phase-A stub
emitter ON the instance (per the standing rule: Cloud Shell is a control surface
only; workloads never run in Cloud Shell). No GPU, no inference -- this proves
acquisition / wiring / teardown for ~$0.

Billing matrix: GCP => `instances delete` (delete, not stop).
Every instance is labelled `gpuha-managed=<run-id>` so reap can find it with no
local state. gcloud is invoked as a subprocess; the plane is authed to the
project (or the agent drives gcloud via Cloud Shell). Requires topology pool
`extra.zone` and the plane telem endpoint passed at construction.
"""
import json, os, subprocess, tempfile, time
from .base import ProviderAdapter, CapacityError, AdapterError
from ..state import ResourceHandle

STARTUP_TMPL = """#!/bin/bash
set -e
mkdir -p /opt/gpuha && cd /opt/gpuha
for f in stub_emitter.py telemetry.py; do
  curl -fsS "http://{plane_ip}:{http_port}/$f" -o "$f"
done
cat > /etc/systemd/system/gpuha-stub.service <<UNIT
[Unit]
Description=GPU HA stub emitter (Phase O managed)
After=network-online.target
[Service]
ExecStart=/usr/bin/python3 /opt/gpuha/stub_emitter.py --node-id {node_id} --backend {backend} --region {region} --dest {plane_ip}:{telem_port} --interval 1.0
Restart=always
[Install]
WantedBy=multi-user.target
UNIT
systemctl daemon-reload
systemctl enable --now gpuha-stub.service
"""


class GCPStubAdapter(ProviderAdapter):
    name = "gcp_stub"

    def __init__(self, plane_ip="203.0.113.10", telem_port=5106,
                 http_port=8090, project=None, dry_run=False, **kw):
        self.plane_ip = plane_ip
        self.telem_port = telem_port
        self.http_port = http_port
        self.project = project
        self.dry_run = dry_run

    def _gcloud(self, args, capture=True):
        cmd = ["gcloud"] + args + ["--format=json", "--quiet"]
        if self.project:
            cmd += ["--project", self.project]
        if self.dry_run:
            print("[dry-run] " + " ".join(cmd))
            return {}
        p = subprocess.run(cmd, capture_output=capture, text=True)
        if p.returncode != 0:
            err = (p.stderr or "").lower()
            if "quota" in err or "does not have enough resources" in err or "zone_resource_pool_exhausted" in err:
                raise CapacityError(p.stderr.strip())
            raise AdapterError("gcloud failed: %s" % (p.stderr or p.stdout).strip())
        if capture and p.stdout.strip():
            try:
                return json.loads(p.stdout)
            except json.JSONDecodeError:
                return {}
        return {}

    def acquire(self, pool_spec, run_id):
        zone = (pool_spec.extra or {}).get("zone") or (pool_spec.region + "-b")
        mt = pool_spec.machine_type or "e2-micro"
        handles = []
        for w in range(pool_spec.workers):
            name = "gpuha-%s-%s-%d" % (pool_spec.name.replace("gpuha-target-", ""),
                                       run_id.split("-")[-1], w)
            node_id = "%s-%d" % (pool_spec.name, w)
            startup = STARTUP_TMPL.format(
                plane_ip=self.plane_ip, http_port=self.http_port,
                node_id=node_id, backend=pool_spec.backend,
                region=pool_spec.region, telem_port=self.telem_port)
            fd, sfile = tempfile.mkstemp(prefix="gpuha-startup-", suffix=".sh")
            with os.fdopen(fd, "w") as f:
                f.write(startup)
            try:
                args = ["compute", "instances", "create", name,
                        "--zone", zone, "--machine-type", mt,
                        "--image-family", "debian-12", "--image-project", "debian-cloud",
                        "--labels", "gpuha-managed=%s,gpuha-role=%s" % (run_id, pool_spec.role),
                        "--metadata-from-file", "startup-script=" + sfile]
                out = self._gcloud(args)
            finally:
                os.remove(sfile)
            ip = ""
            try:
                ip = out[0]["networkInterfaces"][0]["accessConfigs"][0]["natIP"]
            except (KeyError, IndexError, TypeError):
                ip = self._external_ip(name, zone)
            handles.append(ResourceHandle(
                provider="gcp_stub", kind="instance", id=name, pool=pool_spec.name,
                role=pool_spec.role, region=pool_spec.region, public_ip=ip,
                tag=self.tag_for(run_id), extra={"zone": zone}))
        return handles

    def _external_ip(self, name, zone):
        out = self._gcloud(["compute", "instances", "describe", name, "--zone", zone])
        try:
            return out["networkInterfaces"][0]["accessConfigs"][0]["natIP"]
        except (KeyError, IndexError, TypeError):
            return ""

    def bootstrap(self, handle, role):
        return

    def verify(self, handle):
        if self.dry_run:
            return True
        out = self._gcloud(["compute", "instances", "describe", handle.id,
                            "--zone", handle.extra.get("zone", "")])
        return isinstance(out, dict) and out.get("status") == "RUNNING"

    def teardown(self, handle):
        self._gcloud(["compute", "instances", "delete", handle.id,
                     "--zone", handle.extra.get("zone", "")], capture=False)

    def reap(self, run_id):
        flt = "labels.gpuha-managed:*" if run_id is None else ("labels.gpuha-managed=" + run_id)
        out = self._gcloud(["compute", "instances", "list", "--filter", flt])
        killed = []
        for inst in (out or []):
            name = inst["name"]
            zone = inst["zone"].split("/")[-1]
            self._gcloud(["compute", "instances", "delete", name, "--zone", zone],
                         capture=False)
            killed.append(name)
        return killed
