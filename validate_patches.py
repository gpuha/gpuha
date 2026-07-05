"""Functional proof of the two new patches: router --degrade (graceful whale
completion instead of bare 503) and emitter fan-out (repeatable --dest/--telemetry)."""
import subprocess, sys, socket, time, json, os
DN=subprocess.DEVNULL
def start_router(port, tport, degrade=None):
    cmd=[sys.executable,"router.py","--port",str(port),"--telemetry-port",str(tport)]
    if degrade: cmd+=["--degrade",degrade]
    return subprocess.Popen(cmd,stdout=DN,stderr=DN)
def post(port, body):
    s=socket.create_connection(("127.0.0.1",port),timeout=4)
    req=(b"POST /v1/chat/completions HTTP/1.1\r\nHost: x\r\nContent-Type: application/json\r\n"
         b"Content-Length: %d\r\nConnection: close\r\n\r\n"%len(body))+body
    s.sendall(req); resp=b""
    while True:
        d=s.recv(65536)
        if not d: break
        resp+=d
    s.close()
    head,_,rest=resp.partition(b"\r\n\r\n"); status=int(head.split()[1])
    hdr={}
    for ln in head.split(b"\r\n")[1:]:
        k,_,v=ln.partition(b": "); hdr[k.decode().lower()]=v.decode()
    return status,hdr,rest
res=[]
def chk(n,ok,extra=""): res.append(ok); print(f"[{'PASS' if ok else 'FAIL'}] {n}  {extra}")

# --- router --degrade error : 503 + Retry-After + spec code ---
p=start_router(9401,5401,"error"); time.sleep(1.2)
try:
    st,h,body=post(9401,b'{"model":"gpuha","messages":[]}')
    err=json.loads(body) if body.strip().startswith(b'{') else {}
    chk("degrade=error -> 503 + Retry-After + gpuha_all_pools_down",
        st==503 and h.get("retry-after")=="30" and err.get("error",{}).get("code")=="gpuha_all_pools_down",
        f"(status={st} retry={h.get('retry-after')})")
finally: p.terminate(); p.wait()

# --- router --degrade auto : 200 graceful (non-stream) + stream ---
p=start_router(9402,5402,"auto"); time.sleep(1.2)
try:
    st,h,body=post(9402,b'{"model":"gpuha","messages":[]}')
    obj=json.loads(body)
    chk("degrade=auto non-stream -> 200 chat.completion model=gpuha-degraded",
        st==200 and obj.get("model")=="gpuha-degraded" and obj["choices"][0]["finish_reason"]=="stop",
        f"(status={st})")
    st2,h2,body2=post(9402,b'{"model":"gpuha","stream": true,"messages":[]}')
    chk("degrade=auto stream:true -> 200 SSE ending [DONE]",
        st2==200 and b"text/event-stream" in (h2.get("content-type","").encode()+b"") and b"[DONE]" in body2,
        f"(status={st2})")
finally: p.terminate(); p.wait()

# --- no --degrade : bare 503 gpuha_no_capacity (unchanged default) ---
p=start_router(9403,5403,None); time.sleep(1.2)
try:
    st,h,body=post(9403,b'{"messages":[]}')
    err=json.loads(body)
    chk("no --degrade -> bare 503 gpuha_no_capacity (default unchanged)",
        st==503 and err["error"]["type"]=="gpuha_no_capacity", f"(status={st})")
finally: p.terminate(); p.wait()

# --- fake_worker fan-out : both dests receive frames ---
socks=[]
for prt in (5501,5502):
    s=socket.socket(socket.AF_INET,socket.SOCK_DGRAM); s.bind(("127.0.0.1",prt)); s.settimeout(4); socks.append((prt,s))
fw=subprocess.Popen([sys.executable,"fake_worker.py","--id","fw1","--port","8501",
    "--telemetry","127.0.0.1:5501","--telemetry","127.0.0.1:5502"],stdout=DN,stderr=DN)
time.sleep(2.5); got={}
for prt,s in socks:
    try: d,_=s.recvfrom(4096); got[prt]=json.loads(d.decode())["node_id"]
    except socket.timeout: got[prt]=None
    s.close()
fw.terminate(); fw.wait()
chk("fake_worker fan-out -> BOTH dests receive frames", got.get(5501)=="fw1" and got.get(5502)=="fw1", str(got))

print("\nRESULT:", "ALL PASS" if all(res) else "FAILURES PRESENT")
