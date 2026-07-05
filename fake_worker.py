import asyncio, json, time, argparse

class FakeWorker:
    def __init__(self, worker_id, backend="gpuha-target-local", region="us-east1", model="fake-3b"):
        self.worker_id = worker_id; self.backend = backend; self.region = region
        self.model = model; self.fail_mode = "none"; self.first_token_delay = 0.05
        self.vram_used_frac = 0.30; self.queue_depth = 0; self.price_per_hr = 1.0
        self._seq = 0; self.emitting = True
    def ttft_ms(self):
        return self.first_token_delay * 1000 * (8 if self.fail_mode == "slow" else 1)
    def next_frame(self):
        from telemetry import TelemetryFrame
        self._seq += 1
        return TelemetryFrame(node_id=self.worker_id, backend=self.backend, region=self.region,
            ts_unix=time.time(), seq=self._seq, vram_used_frac=self.vram_used_frac,
            ttft_ms=self.ttft_ms(), queue_depth=self.queue_depth, price_usd_hr=self.price_per_hr,
            model=self.model)

class TelemetryEmitter(asyncio.DatagramProtocol):
    def __init__(self, worker, dests, interval=1.0):
        self.worker = worker; self.dests = dests; self.interval = interval; self.transport = None
    def connection_made(self, transport): self.transport = transport
    async def run(self):
        while True:
            if self.worker.emitting and self.transport is not None:
                try:
                    frame = self.worker.next_frame().to_bytes()
                    for d in self.dests: self.transport.sendto(frame, d)
                except Exception: pass
            await asyncio.sleep(self.interval)

async def read_http_request(reader):
    request_line = await reader.readline()
    if not request_line: return None
    parts = request_line.decode("latin1").split()
    if len(parts) < 2: return None
    method, path = parts[0], parts[1]
    headers = {}
    while True:
        line = await reader.readline()
        if line in (b"\r\n", b"\n", b""): break
        k, _, v = line.decode("latin1").partition(":")
        headers[k.strip().lower()] = v.strip()
    body = b""
    if "content-length" in headers:
        body = await reader.readexactly(int(headers["content-length"]))
    return method, path, headers, body

def http_response_head(status, content_type, extra=None, chunked=False):
    reason = {200:"OK",503:"Service Unavailable",404:"Not Found"}.get(status,"OK")
    lines = [f"HTTP/1.1 {status} {reason}", f"Content-Type: {content_type}", "Cache-Control: no-cache"]
    if chunked: lines.append("Transfer-Encoding: chunked")
    if extra:
        for k, v in extra.items(): lines.append(f"{k}: {v}")
    lines.append("Connection: close")
    return ("\r\n".join(lines) + "\r\n\r\n").encode()

def chunk(data): return f"{len(data):X}\r\n".encode() + data + b"\r\n"

def make_handler(worker):
    async def handle(reader, writer):
        try:
            req = await read_http_request(reader)
            if req is None: writer.close(); return
            method, path, headers, body = req
            if method == "GET" and path.startswith("/__control"):
                q = {}
                if "?" in path:
                    for kv in path.split("?",1)[1].split("&"):
                        if "=" in kv: k,v = kv.split("=",1); q[k]=v
                if "fail_mode" in q: worker.fail_mode = q["fail_mode"]
                if "vram" in q: worker.vram_used_frac = float(q["vram"])
                if "price" in q: worker.price_per_hr = float(q["price"])
                if "emitting" in q: worker.emitting = (q["emitting"]=="1")
                payload = json.dumps({"worker_id":worker.worker_id,"fail_mode":worker.fail_mode}).encode()
                writer.write(http_response_head(200,"application/json",{"Content-Length":str(len(payload))}))
                writer.write(payload); await writer.drain(); writer.close(); return
            if method == "POST" and path.startswith("/v1/chat/completions"):
                worker.queue_depth += 1
                try:
                    if worker.fail_mode == "before_token":
                        msg = b"worker crashed before first token"
                        writer.write(http_response_head(503,"text/plain",{"Content-Length":str(len(msg)),"X-GPUHA-Worker":worker.worker_id}))
                        writer.write(msg); await writer.drain(); writer.close(); return
                    writer.write(http_response_head(200,"text/event-stream",{"X-GPUHA-Worker":worker.worker_id},chunked=True))
                    await writer.drain()
                    delay = worker.first_token_delay * (8 if worker.fail_mode=="slow" else 1)
                    tokens = ["Hello",","," I"," am"," worker",f" {worker.worker_id}",".","Here","is","a","reply","."]
                    for i, tok in enumerate(tokens):
                        await asyncio.sleep(delay if i==0 else 0.02)
                        if worker.fail_mode == "after_token" and i == 3:
                            writer.close(); return
                        sse = f"data: {json.dumps({'id':f'cmpl-{worker.worker_id}','choices':[{'delta':{'content':tok}}]})}\n\n"
                        writer.write(chunk(sse.encode())); await writer.drain()
                    writer.write(chunk(b"data: [DONE]\n\n")); writer.write(b"0\r\n\r\n")
                    await writer.drain(); writer.close()
                finally: worker.queue_depth -= 1
                return
            writer.write(http_response_head(404,"text/plain",{"Content-Length":"0"}))
            await writer.drain(); writer.close()
        except (ConnectionResetError, BrokenPipeError): pass
        except Exception:
            try: writer.close()
            except Exception: pass
    return handle

async def serve(worker_id, port, telemetry_dests=None, backend="gpuha-target-local", region="us-east1"):
    worker = FakeWorker(worker_id, backend=backend, region=region)
    if telemetry_dests:
        loop = asyncio.get_running_loop()
        emitter = TelemetryEmitter(worker, telemetry_dests, interval=1.0)
        await loop.create_datagram_endpoint(lambda: emitter, local_addr=("0.0.0.0", 0))
        asyncio.create_task(emitter.run())
    server = await asyncio.start_server(make_handler(worker), "0.0.0.0", port)
    async with server: await server.serve_forever()

if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--id", required=True); ap.add_argument("--port", type=int, required=True)
    ap.add_argument("--telemetry", action="append", default=None,
                    help="router UDP host:port (repeatable = fan-out)")
    ap.add_argument("--backend", default="gpuha-target-local")
    ap.add_argument("--region", default="us-east1")
    args = ap.parse_args()
    dests = None
    if args.telemetry:
        dests = [(h, int(p)) for h, p in (d.split(":") for d in args.telemetry)]
    asyncio.run(serve(args.id, args.port, dests, args.backend, args.region))
