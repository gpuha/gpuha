# Two-Nanode cross-internet transport drill (closes D.5)
1. Nanode 1GB in a DIFFERENT region (Dallas/Fremont), label gpuha-emitter-tmp (~$0.008/hr).
2. Cloud Firewall: open plane UDP/5106 scoped to emitter /32 (remove after).
3. Emitter box:  python3 stub_emitter.py --node-id linode-west --backend gpuha-target-linode-west --dest <PLANE_IP>:5106
   Plane also runs a local emitter for gpuha-target-plane (127.0.0.1:5106).
4. Dig from Scott's machine:  dig @<PLANE_IP> -p 5353 api.gpuha.com A
   | # | action | expect |
   |---|--------|--------|
   | 1 | both emitters up | both pools in answer (cross-internet inclusion) |
   | 2 | stop remote emitter | remote pool gone <=10s (cross-internet evacuation) |
   | 3 | restart remote | restored |
   | 4 | stop both | FAILSAFE -> answer = WHALE IP; `curl http://<PLANE_IP>:8080/v1/chat/completions -d '{"messages":[]}'` -> graceful degraded completion |
   | 5 | throughout | report dropped_frames (first genuine internet UDP-loss numbers) |
5. DELETE gpuha-emitter-tmp (delete, not stop). Remove the UDP/5106 firewall rule. Plane stays up.
