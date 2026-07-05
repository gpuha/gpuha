"""
GPU HA — whale.py: the last-resort endpoint ("fail whale" for LLM traffic)
==========================================================================

When every pool is dark, Tier-1's FAILSAFE points here instead of at dead
router IPs. The whale always answers, speaks the OpenAI protocol, and keeps
the end-user experience graceful instead of connection-refused.

GOVERNING PRINCIPLE: the whale is the DUMBEST thing in the fleet.
No telemetry, no state, no disk I/O per request, no dependencies. Every
response is a byte buffer precomputed at startup; the request handler does
nothing but a substring sniff and sendall(). A last resort that shares fate
with the things it's a last resort FOR is decoration.

Modes (--mode):
  auto      (default) request has "stream": true -> streamed SSE graceful
            completion; otherwise JSON graceful completion. 200s.
  complete  always the JSON graceful completion (no streaming).
  error     protocol-correct degradation: 503 + spec-shaped error JSON +
            Retry-After header. Official SDKs retry/backoff on this
            automatically — the self-healing path for API clients.

In-band degradation signal: the `model` field of every whale completion is
"gpuha-degraded" (configurable). Chat frontends render the polite message;
programmatic clients detect degradation from a field they already parse.

Stdlib only. Scale-out: --workers N uses SO_REUSEPORT.
"""

import asyncio
import argparse
import json
import os
import socket
import time


# ---------------------------------------------------------------------------
# Precomputed responses — built once at startup, sent verbatim forever after.
# ---------------------------------------------------------------------------

def _head(status: int, ctype: str, extra: dict, body_len: int | None) -> bytes:
    reason = {200: "OK", 404: "Not Found", 503: "Service Unavailable"}[status]
    lines = [f"HTTP/1.1 {status} {reason}",
             f"Content-Type: {ctype}",
             "Cache-Control: no-store",
             # Browser chat apps call OpenAI-compatible endpoints directly.
             "Access-Control-Allow-Origin: *",
             "Access-Control-Allow-Headers: *",
             "Access-Control-Allow-Methods: POST, GET, OPTIONS",
             "Connection: close"]
    if body_len is not None:
        lines.append(f"Content-Length: {body_len}")
    for k, v in extra.items():
        lines.append(f"{k}: {v}")
    return ("\r\n".join(lines) + "\r\n\r\n").encode()


def build_responses(message: str, model: str, retry_after: int) -> dict:
    now = int(time.time())  # frozen at startup; nobody checks, and dumb is the point

    # --- 200 JSON graceful completion (non-streaming) ---
    completion = {
        "id": "chatcmpl-gpuha-degraded",
        "object": "chat.completion",
        "created": now,
        "model": model,
        "choices": [{
            "index": 0,
            "message": {"role": "assistant", "content": message},
            "finish_reason": "stop",
        }],
        "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
    }
    body = json.dumps(completion, separators=(",", ":")).encode()
    json_200 = _head(200, "application/json", {"X-GPUHA-Degraded": "true"}, len(body)) + body

    # --- 200 SSE streamed graceful completion ---
    def sse(obj) -> bytes:
        data = f"data: {json.dumps(obj, separators=(',', ':'))}\n\n".encode()
        return f"{len(data):X}\r\n".encode() + data + b"\r\n"

    chunks = []
    base = {"id": "chatcmpl-gpuha-degraded", "object": "chat.completion.chunk",
            "created": now, "model": model}
    chunks.append(sse({**base, "choices": [{"index": 0,
        "delta": {"role": "assistant", "content": ""}, "finish_reason": None}]}))
    # Send the message in a few word-ish chunks so streaming UIs render naturally.
    words = message.split(" ")
    step = max(1, len(words) // 6)
    for i in range(0, len(words), step):
        piece = (" " if i else "") + " ".join(words[i:i + step])
        chunks.append(sse({**base, "choices": [{"index": 0,
            "delta": {"content": piece}, "finish_reason": None}]}))
    chunks.append(sse({**base, "choices": [{"index": 0, "delta": {},
                                            "finish_reason": "stop"}]}))
    done = b"data: [DONE]\n\n"
    chunks.append(f"{len(done):X}\r\n".encode() + done + b"\r\n")
    chunks.append(b"0\r\n\r\n")
    sse_200 = (_head(200, "text/event-stream",
                     {"X-GPUHA-Degraded": "true", "Transfer-Encoding": "chunked"},
                     None)
               + b"".join(chunks))

    # --- 503 protocol-correct degradation ---
    err = {"error": {"message": message, "type": "service_unavailable",
                     "code": "gpuha_all_pools_down"}}
    ebody = json.dumps(err, separators=(",", ":")).encode()
    err_503 = _head(503, "application/json",
                    {"Retry-After": str(retry_after), "X-GPUHA-Degraded": "true"},
                    len(ebody)) + ebody

    # --- misc ---
    hbody = b'{"status":"whale","degraded":true}'
    health_200 = _head(200, "application/json", {}, len(hbody)) + hbody
    nf_404 = _head(404, "application/json", {}, 2) + b"{}"
    opt_200 = _head(200, "text/plain", {}, 0)

    return {"json": json_200, "sse": sse_200, "err": err_503,
            "health": health_200, "nf": nf_404, "options": opt_200}


# ---------------------------------------------------------------------------
# Server — read just enough of the request to route; sendall; close.
# ---------------------------------------------------------------------------

class Whale:
    def __init__(self, responses: dict, mode: str):
        self.r = responses
        self.mode = mode
        self.served = 0

    async def handle(self, reader: asyncio.StreamReader,
                     writer: asyncio.StreamWriter):
        try:
            # Read request head + as much body as arrives promptly. We never
            # need the full body — just the path and a substring sniff.
            raw = await asyncio.wait_for(reader.read(8192), timeout=2.0)
            if not raw:
                writer.close(); return
            head = raw.split(b"\r\n", 1)[0]
            parts = head.split()
            method = parts[0] if parts else b""
            path = parts[1] if len(parts) > 1 else b"/"

            self.served += 1
            if method == b"OPTIONS":
                writer.write(self.r["options"])
            elif path.startswith(b"/healthz"):
                writer.write(self.r["health"])
            elif path.startswith(b"/v1/chat/completions") or \
                    path.startswith(b"/v1/completions"):
                if self.mode == "error":
                    writer.write(self.r["err"])
                elif self.mode == "complete":
                    writer.write(self.r["json"])
                else:  # auto: substring sniff — no JSON parse at panic volume
                    if b'"stream": true' in raw or b'"stream":true' in raw:
                        writer.write(self.r["sse"])
                    else:
                        writer.write(self.r["json"])
            else:
                writer.write(self.r["nf"])
            await writer.drain()
            writer.close()
        except Exception:
            try: writer.close()
            except Exception: pass


async def serve(port: int, mode: str, message: str, model: str,
                retry_after: int, ready_evt: asyncio.Event | None = None):
    whale = Whale(build_responses(message, model, retry_after), mode)
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    if hasattr(socket, "SO_REUSEPORT"):
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
    sock.bind(("0.0.0.0", port))
    server = await asyncio.start_server(whale.handle, sock=sock)
    if ready_evt is not None:
        ready_evt.set()
    async with server:
        await server.serve_forever()


DEFAULT_MESSAGE = ("I'm having trouble reaching compute resources right now. "
                   "Please give me a moment and try again shortly.")

if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8080)
    ap.add_argument("--mode", choices=["auto", "complete", "error"],
                    default="auto")
    ap.add_argument("--message", default=DEFAULT_MESSAGE)
    ap.add_argument("--model-name", default="gpuha-degraded")
    ap.add_argument("--retry-after", type=int, default=30)
    ap.add_argument("--workers", type=int, default=1,
                    help="SO_REUSEPORT process count for scale-out")
    args = ap.parse_args()

    if args.workers > 1:
        for _ in range(args.workers - 1):
            if os.fork() == 0:
                break
    asyncio.run(serve(args.port, args.mode, args.message,
                      args.model_name, args.retry_after))
