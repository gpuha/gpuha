import asyncio, json, time, argparse

# grey-failure fix: an accept-then-hang upstream (e.g. a terminating host)
# must be treated as a PRE-TOKEN failure and retried, never left to ride
# the client timeout. Deadline covers connect + first token per attempt.
PRE_TOKEN_DEADLINE = 4.0
from selection import Worker, WorkerSelector, SelectorConfig
from telemetry import TelemetryIngest
import fake_worker as fw
from whale import build_responses, DEFAULT_MESSAGE

class TelemetryListener(asyncio.DatagramProtocol):
    def __init__(self, ingest): self.ingest = ingest
    def datagram_received(self, data, addr):
        self.ingest.ingest(data, now_monotonic=time.monotonic())

class Router:
    def __init__(self, backends, config=None, degrade=None, auth=None):
        self.degrade = degrade
        self.backend_auth = auth or {}
        self._whale = build_responses(DEFAULT_MESSAGE, "gpuha-degraded", 30) if degrade else None
        self.backends = backends
        self.cfg = config or SelectorConfig()
        self.selector = WorkerSelector(config=self.cfg)
        for wid in backends:
            self.selector.upsert(Worker(id=wid, last_seen=0.0))
        self.ingest = TelemetryIngest()
        self.stats = {"requests":0,"failovers":0,"midstream_failfast":0,"no_capacity":0,"ok":0}

    async def _open_upstream(self, host, port, body, auth=None):
        reader, writer = await asyncio.open_connection(host, port)
        auth_hdr = (f"Authorization: Bearer {auth}\r\n" if auth else "")
        req = (f"POST /v1/chat/completions HTTP/1.1\r\nHost: {host}:{port}\r\n"
               f"{auth_hdr}"
               f"Content-Type: application/json\r\nContent-Length: {len(body)}\r\n"
               f"Connection: close\r\n\r\n").encode() + body
        writer.write(req); await writer.drain()
        status_line = await reader.readline()
        if not status_line: raise ConnectionError("no status line")
        status = int(status_line.decode("latin1").split()[1])
        headers = {}
        while True:
            line = await reader.readline()
            if line in (b"\r\n", b"\n", b""): break
            k, _, v = line.decode("latin1").partition(":")
            headers[k.strip().lower()] = v.strip()
        return status, headers, reader, writer

    async def _read_body(self, reader, headers):
        te = headers.get("transfer-encoding", "").lower()
        if "chunked" in te:
            async for data in self._iter_chunks(reader):
                yield data
        else:
            remaining = int(headers.get("content-length", 0))
            while remaining > 0:
                data = await reader.read(min(65536, remaining))
                if not data:
                    break
                remaining -= len(data)
                yield data

    async def _iter_chunks(self, reader):
        while True:
            size_line = await reader.readline()
            if not size_line: raise ConnectionError("upstream dropped")
            try: size = int(size_line.strip().split(b";")[0], 16)
            except ValueError: raise ConnectionError("bad chunk size")
            if size == 0:
                await reader.readline(); return
            data = await reader.readexactly(size)
            await reader.readline()
            yield data

    async def sync_from_ingest(self, interval=0.5):
        while True:
            for node_id, view in list(self.ingest.nodes.items()):
                f = view.frame
                if node_id not in self.backends: continue
                if self.selector.workers.get(node_id) is None:
                    self.selector.upsert(Worker(id=node_id, last_seen=0.0))
                self.selector.update_telemetry(node_id, vram_used_frac=f.vram_used_frac,
                    ttft_ms=f.ttft_ms, queue_depth=f.queue_depth, price_per_hr=f.price_usd_hr,
                    now=view.received_monotonic)
            await asyncio.sleep(interval)

    def _degraded(self, body):
        # precomputed whale buffers; error->503 spec body, auto->200 graceful (sse if streaming)
        if self.degrade == "error":
            return self._whale["err"]
        if b'"stream": true' in body or b'"stream":true' in body:
            return self._whale["sse"]
        return self._whale["json"]

    async def handle(self, client_reader, client_writer):
        try:
            req = await fw.read_http_request(client_reader)
            if req is None: client_writer.close(); return
            method, path, headers, body = req
            if method == "GET" and path.startswith("/__stats"):
                elig = [w.id for w in self.selector.eligible(time.monotonic())]
                payload = json.dumps({"stats":self.stats,"eligible":elig}).encode()
                client_writer.write(fw.http_response_head(200,"application/json",{"Content-Length":str(len(payload))}))
                client_writer.write(payload); await client_writer.drain(); client_writer.close(); return
            if not (method=="POST" and path.startswith("/v1/chat/completions")):
                client_writer.write(fw.http_response_head(404,"text/plain",{"Content-Length":"0"}))
                await client_writer.drain(); client_writer.close(); return
            self.stats["requests"] += 1
            plan = self.selector.select_with_retry_plan(max_attempts=3)
            if not plan:
                self.stats["no_capacity"] += 1
                if self.degrade:
                    client_writer.write(self._degraded(body)); await client_writer.drain(); client_writer.close(); return
                msg = json.dumps({"error":{"type":"gpuha_no_capacity"}}).encode()
                client_writer.write(fw.http_response_head(503,"application/json",{"Content-Length":str(len(msg))}))
                client_writer.write(msg); await client_writer.drain(); client_writer.close(); return
            committed = False
            for attempt, worker in enumerate(plan):
                host, port = self.backends[worker.id]
                up_writer = None
                try:
                    # PRE-TOKEN deadline: connect + first response line. A terminating
                    # host that accepts-then-hangs would otherwise block here forever
                    # with no error, and the client rides its own timeout with no
                    # retry. A breach here is a pre-token failure -> try next-best.
                    status, up_headers, up_reader, up_writer = await asyncio.wait_for(
                        self._open_upstream(host, port, body,
                                            self.backend_auth.get(worker.id)),
                        timeout=PRE_TOKEN_DEADLINE)
                    if status != 200:
                        self.selector.trip_breaker(worker.id); up_writer.close()
                        if attempt+1 < len(plan): self.stats["failovers"] += 1
                        continue
                    body_iter = self._read_body(up_reader, up_headers).__aiter__()
                    while True:
                        try:
                            if not committed:
                                # first token still owes a deadline: a 200 header
                                # then a hung body is still a pre-token stall.
                                data = await asyncio.wait_for(
                                    body_iter.__anext__(), timeout=PRE_TOKEN_DEADLINE)
                            else:
                                data = await body_iter.__anext__()
                        except StopAsyncIteration:
                            break
                        if not committed:
                            client_writer.write(fw.http_response_head(200,"text/event-stream",
                                {"X-GPUHA-Served-By":worker.id,"X-GPUHA-Attempt":str(attempt)},chunked=True))
                            committed = True
                        client_writer.write(fw.chunk(data)); await client_writer.drain()
                    client_writer.write(b"0\r\n\r\n"); await client_writer.drain()
                    up_writer.close(); self.stats["ok"] += 1; client_writer.close(); return
                except (ConnectionError, asyncio.IncompleteReadError, ConnectionResetError, asyncio.TimeoutError):
                    if up_writer is not None:
                        try: up_writer.close()
                        except Exception: pass
                    if committed:
                        self.stats["midstream_failfast"] += 1; self.selector.trip_breaker(worker.id)
                        try: client_writer.write(b"0\r\n\r\n"); await client_writer.drain()
                        except Exception: pass
                        client_writer.close(); return
                    self.selector.trip_breaker(worker.id)
                    if attempt+1 < len(plan): self.stats["failovers"] += 1
                    continue
            if not committed:
                self.stats["no_capacity"] += 1
                if self.degrade:
                    client_writer.write(self._degraded(body)); await client_writer.drain(); client_writer.close(); return
                msg = json.dumps({"error":{"type":"gpuha_all_failed"}}).encode()
                client_writer.write(fw.http_response_head(503,"application/json",{"Content-Length":str(len(msg))}))
                client_writer.write(msg); await client_writer.drain()
            client_writer.close()
        except Exception:
            try: client_writer.close()
            except Exception: pass

async def serve(backends, port, ready_evt=None, telemetry_port=5006, degrade=None, auth=None):
    router = Router(backends, degrade=degrade, auth=auth)
    loop = asyncio.get_running_loop()
    await loop.create_datagram_endpoint(lambda: TelemetryListener(router.ingest),
        local_addr=("0.0.0.0", telemetry_port))
    sync_task = asyncio.create_task(router.sync_from_ingest(interval=0.5))
    server = await asyncio.start_server(router.handle, "0.0.0.0", port)
    if ready_evt is not None: ready_evt.set()
    async with server:
        try: await server.serve_forever()
        finally: sync_task.cancel()

if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=9000)
    ap.add_argument("--telemetry-port", type=int, default=5006)
    ap.add_argument("--backend", action="append", default=[])
    ap.add_argument("--backend-auth", action="append", default=[],
                    help="wid=token: inject Authorization: Bearer token to that backend")
    ap.add_argument("--degrade", choices=["auto","error"], default=None,
                    help="serve graceful whale completion instead of bare 503 when no capacity")
    args = ap.parse_args()
    backends = {}
    for b in args.backend:
        wid, hostport = b.split("=", 1)
        host, port = hostport.split(":")
        backends[wid] = (host, int(port))
    auth = {}
    for a in args.backend_auth:
        wid, tok = a.split("=", 1)
        auth[wid] = tok
    asyncio.run(serve(backends, args.port, telemetry_port=args.telemetry_port, degrade=args.degrade, auth=auth))
