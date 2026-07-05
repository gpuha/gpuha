"""Whale contract tests: valid OpenAI-shaped responses in every mode, plus a
crude single-process throughput sanity check (the whale exists for the moment
ALL traffic converges on it)."""
import asyncio, json, time
from whale import serve, DEFAULT_MESSAGE

async def req(port, body: bytes, path=b"/v1/chat/completions", method=b"POST"):
    r, w = await asyncio.open_connection("127.0.0.1", port)
    w.write(method + b" " + path + b" HTTP/1.1\r\nHost: x\r\nContent-Type: application/json\r\n"
            + f"Content-Length: {len(body)}\r\n\r\n".encode() + body)
    await w.drain()
    raw = await r.read()
    w.close()
    head, _, rest = raw.partition(b"\r\n\r\n")
    status = int(head.split()[1])
    headers = {}
    for ln in head.split(b"\r\n")[1:]:
        k, _, v = ln.partition(b": ")
        headers[k.decode().lower()] = v.decode()
    return status, headers, rest

def dechunk(rest: bytes) -> bytes:
    out, i = b"", 0
    while i < len(rest):
        j = rest.index(b"\r\n", i)
        size = int(rest[i:j], 16)
        if size == 0: break
        out += rest[j+2:j+2+size]; i = j + 2 + size + 2
    return out

async def main():
    ready = asyncio.Event()
    asyncio.create_task(serve(8180, "auto", DEFAULT_MESSAGE, "gpuha-degraded", 30, ready))
    await ready.wait()

    print("=" * 64); print("1. NON-STREAM: valid chat.completion, degraded signal"); print("=" * 64)
    s, h, body = await req(8180, b'{"model":"gpuha","messages":[]}')
    obj = json.loads(body)
    print(f"  status={s} model={obj['model']} degraded_hdr={h.get('x-gpuha-degraded')}")
    print(f"  content: {obj['choices'][0]['message']['content'][:60]}...")
    assert s == 200 and obj["object"] == "chat.completion"
    assert obj["model"] == "gpuha-degraded" and obj["choices"][0]["finish_reason"] == "stop"
    print("  PASS")

    print("=" * 64); print("2. STREAM: valid SSE chunks ending [DONE]"); print("=" * 64)
    s, h, rest = await req(8180, b'{"model":"gpuha","stream": true,"messages":[]}')
    sse = dechunk(rest).decode()
    datas = [l[6:] for l in sse.splitlines() if l.startswith("data: ")]
    assert s == 200 and datas[-1] == "[DONE]"
    text = "".join(json.loads(d)["choices"][0]["delta"].get("content", "")
                   for d in datas[:-1])
    print(f"  status={s} chunks={len(datas)} reassembled: {text[:60]}...")
    assert "trouble reaching compute" in text
    print("  PASS")

    print("=" * 64); print("3. ERROR MODE: 503 + Retry-After (SDK self-heal path)"); print("=" * 64)
    ready2 = asyncio.Event()
    asyncio.create_task(serve(8181, "error", DEFAULT_MESSAGE, "gpuha-degraded", 30, ready2))
    await ready2.wait()
    s, h, body = await req(8181, b'{"messages":[]}')
    err = json.loads(body)
    print(f"  status={s} retry_after={h.get('retry-after')} code={err['error']['code']}")
    assert s == 503 and h.get("retry-after") == "30"
    assert err["error"]["type"] == "service_unavailable"
    print("  PASS")

    print("=" * 64); print("4. THROUGHPUT SANITY: single process, sequential+concurrent"); print("=" * 64)
    N = 2000; t0 = time.monotonic()
    CONC = 50
    async def one():
        await req(8180, b'{"messages":[]}')
    for batch in range(N // CONC):
        await asyncio.gather(*[one() for _ in range(CONC)])
    dt = time.monotonic() - t0
    print(f"  {N} requests, concurrency {CONC}: {dt:.2f}s -> {N/dt:,.0f} req/s (single process)")
    assert N / dt > 500, "should comfortably exceed 500 rps even in a sandbox"
    print("  PASS  (production: --workers N + nginx front if ever needed)")

    print(); print("ALL WHALE CONTRACT TESTS PASSED")

asyncio.run(main())
