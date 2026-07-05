#!/bin/bash
# INDEPENDENT dead-man reaper: nuke ALL gpuha-managed across providers. No orchestrator dep.
LOG=/root/gpuha-runs/deadman.log
echo "=== DEADMAN FIRED $(date -u) ===" >> "$LOG"
set -a; . /root/gpuha-workbench/gpuha/.env 2>/dev/null; set +a
API=https://cloud.lambdalabs.com/api/v1
for attempt in 1 2 3 4 5 6 7 8; do
  IDS=$(curl -s -u "$LAMBDA_API_KEY:" "$API/instances" | python3 -c "import json,sys
d=json.load(sys.stdin).get('data',[])
print(','.join(i['id'] for i in d if (i.get('name') or '').startswith('gpuha-managed--')))" 2>>"$LOG")
  echo "[$(date -u +%T)] attempt $attempt lambda gpuha ids: [$IDS]" >> "$LOG"
  if [ -z "$IDS" ]; then echo "lambda clean" >> "$LOG"; break; fi
  BODY=$(python3 -c "import json,sys;print(json.dumps({'instance_ids':sys.argv[1].split()}))" "$IDS")
  curl -s -u "$LAMBDA_API_KEY:" -X POST "$API/instance-operations/terminate" -H "Content-Type: application/json" -d "$BODY" >> "$LOG" 2>&1
  echo "" >> "$LOG"; sleep 20
done
# RunPod: DELETE gpuha-managed pods (delete, never stop)
RP=https://rest.runpod.io/v1
for attempt in 1 2 3 4 5; do
  PIDS=$(curl -s -H "Authorization: Bearer $RUNPOD_API_KEY" "$RP/pods" | python3 -c "import json,sys
d=json.load(sys.stdin)
d=d.get('data',d) if isinstance(d,dict) else d
print(' '.join(p['id'] for p in d if (p.get('name') or '').startswith('gpuha-managed--')))" 2>>"$LOG")
  echo "[$(date -u +%T)] attempt $attempt runpod gpuha ids: [$PIDS]" >> "$LOG"
  if [ -z "$PIDS" ]; then echo "runpod clean" >> "$LOG"; break; fi
  for pid in $PIDS; do curl -s -X DELETE -H "Authorization: Bearer $RUNPOD_API_KEY" "$RP/pods/$pid" >> "$LOG" 2>&1; echo " deleted runpod $pid" >> "$LOG"; done
  sleep 15
done
# GCP: delete gpuha-managed instances
gcloud compute instances list --filter="labels.gpuha-managed:*" --format="value(name,zone)" 2>>"$LOG" | while read N Z; do
  [ -n "$N" ] && gcloud compute instances delete "$N" --zone "$Z" --quiet >> "$LOG" 2>&1 && echo "gcp deleted $N" >> "$LOG"
done
# Backstop: orchestrator reap (picks up any provider in KNOWN_PROVIDERS)
cd /root/gpuha-workbench/gpuha && PYTHONUNBUFFERED=1 ./gpuha reap --all >> "$LOG" 2>&1
curl -s -u "$LAMBDA_API_KEY:" "$API/instances" | python3 -c "import json,sys;print('FINAL lambda instances:', [i.get('name') for i in json.load(sys.stdin).get('data',[])])" >> "$LOG" 2>&1
echo "=== DEADMAN DONE $(date -u) ===" >> "$LOG"
