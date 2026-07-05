"""Proves tier1_dns.py --failsafe-ip: with a whale configured, zero fresh pools
=> DNS answers the whale IP ALONE (not the full pool set), and logs FAILSAFE->WHALE.
With pools fresh, normal pool answers are unaffected."""
import socket, struct, subprocess, sys, time, os
DNS, TEL, FRESH = 5353, 5106, 2.0
WHALE = "203.0.113.9"; A="10.0.0.1"; B="10.0.0.2"; BEA="gpuha-target-gcp-east"; BEB="gpuha-target-runpod"
HERE=os.path.dirname(os.path.abspath(__file__)); sys.path.insert(0,HERE)
def emit(node,be,seq):
    f={"node_id":node,"backend":be,"region":"r","ts_unix":time.time(),"seq":seq,"vram_used_frac":0.1,"ttft_ms":40.0,"queue_depth":0,"price_usd_hr":0.4,"model":"gpuha","max_concurrency":0,"v":1}
    s=socket.socket(socket.AF_INET,socket.SOCK_DGRAM); s.sendto(str.encode(__import__("json").dumps(f,separators=(",",":"))),("127.0.0.1",TEL)); s.close()
def dig():
    q=struct.pack(">HHHHHH",0x1234,0x0100,1,0,0,0)
    for l in "api.gpuha.com".split("."): q+=bytes([len(l)])+l.encode()
    q+=b"\x00"+struct.pack(">HH",1,1)
    s=socket.socket(socket.AF_INET,socket.SOCK_DGRAM); s.settimeout(2); s.sendto(q,("127.0.0.1",DNS))
    resp,_=s.recvfrom(4096); an=struct.unpack(">H",resp[6:8])[0]; off=12
    while resp[off]!=0: off+=1+resp[off]
    off+=5; ips=[]
    for _ in range(an):
        off+=2; rt,rc,ttl,rl=struct.unpack(">HHIH",resp[off:off+10]); off+=10; rd=resp[off:off+rl]; off+=rl
        if rt==1: ips.append(socket.inet_ntoa(rd))
    return sorted(ips)
logf=open(os.path.join(HERE,"failsafe_ip.log"),"w")
proc=subprocess.Popen([sys.executable,"tier1_dns.py","--dns-port",str(DNS),"--telem-port",str(TEL),
    "--fresh",str(FRESH),"--failsafe-ip",WHALE,"--pool","%s=%s"%(BEA,A),"--pool","%s=%s"%(BEB,B)],
    cwd=HERE,stdout=logf,stderr=subprocess.STDOUT)
time.sleep(1.2); res=[]
def chk(n,got,want): ok=got==want; res.append(ok); print("[%s] %-34s got=%s want=%s"%("PASS" if ok else "FAIL",n,got,want))
try:
    for i in range(6): emit("n1",BEA,100+i); emit("n2",BEB,100+i); time.sleep(0.15)
    chk("pools fresh -> normal answer", dig(), sorted([A,B]))
    time.sleep(FRESH+1.2)   # let both pools go dark
    chk("all dark + --failsafe-ip -> WHALE alone", dig(), [WHALE])
    logf.flush(); os.fsync(logf.fileno())
    chk("logged FAILSAFE->WHALE", "FAILSAFE->WHALE %s"%WHALE in open(os.path.join(HERE,"failsafe_ip.log")).read(), True)
    print("\nRESULT:", "ALL PASS" if all(res) else "FAILURES PRESENT")
finally:
    proc.terminate()
    try: proc.wait(timeout=3)
    except Exception: proc.kill()
