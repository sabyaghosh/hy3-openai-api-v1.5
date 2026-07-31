"""
OpenAI-compatible API server for Tencent Hy3 (295B MoE) via HuggingFace Gradio API.
No API key required. Drop-in replacement for OpenAI base_url.

Run:
    pip install fastapi uvicorn httpx pydantic
    python server.py --port 8000

Use with the OpenAI Python SDK:
    from openai import OpenAI
    client = OpenAI(base_url="http://localhost:8000/v1", api_key="anything")
    resp = client.chat.completions.create(
        model="hy3",
        messages=[{"role": "user", "content": "Hello!"}]
    )
    print(resp.choices[0].message.content)

Streaming:
    stream = client.chat.completions.create(
        model="hy3-think",  # enables thinking mode
        messages=[{"role": "user", "content": "Prove 1+1=2"}],
        stream=True,
    )
    for chunk in stream:
        delta = chunk.choices[0].delta
        if getattr(delta, "reasoning_content", None):
            print(delta.reasoning_content, end="", flush=True)
        if delta.content:
            print(delta.content, end="", flush=True)

Tool calling:
    resp = client.chat.completions.create(
        model="hy3",
        messages=[{"role": "user", "content": "Weather in Tokyo?"}],
        tools=[{
            "type": "function",
            "function": {
                "name": "get_weather",
                "description": "Get weather for a city",
                "parameters": {
                    "type": "object",
                    "properties": {"location": {"type": "string"}},
                    "required": ["location"],
                },
            },
        }],
    )
    # resp.choices[0].message.tool_calls[0].function.name == "get_weather"
"""

import argparse
import asyncio
import json
import os
import time
import uuid
from typing import Any, AsyncIterator, Optional

import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel

from logging_layer import (
    RequestRecord,
    get_log_summary,
    get_recent_logs,
    get_recent_requests,
    log_event,
    new_request_id,
)

HY3_BASE = "https://tencent-Hy3.hf.space/gradio_api/call/chat"
DEFAULT_MODEL = "hy3"
DEFAULT_MAX_TOKENS = 262144
DEFAULT_THINK_LEVEL = "no_think"
PRESERVED_THINKING = True
HTTP_TIMEOUT = httpx.Timeout(300.0, connect=30.0)

# Concurrency control — prevents upstream Hy3 overload and gateway timeouts.
# Tune via env vars:
#   MAX_CONCURRENT  — hard cap of in-flight Hy3 calls (default 10)
#   QUEUE_TIMEOUT   — seconds to wait for a slot before returning 503 (default 5.0)
#                     Set to 0 for non-blocking (immediate 503 when at capacity).
MAX_CONCURRENT = int(os.environ.get("MAX_CONCURRENT", "10"))
QUEUE_TIMEOUT = float(os.environ.get("QUEUE_TIMEOUT", "5.0"))

app = FastAPI(
    title="Hy3 OpenAI-Compatible API",
    version="1.2.0",
    description="OpenAI-compatible proxy for Tencent Hy3 295B MoE via HuggingFace Gradio API.",
)

# CORS — allow the Next.js admin panel (and any OpenAI client) to call this API.
# Configure allowed origins via ADMIN_ORIGIN env var (default: permissive for dev).
_admin_origin = os.environ.get("ADMIN_ORIGIN", "*")
app.add_middleware(
    CORSMiddleware,
    allow_origins=[_admin_origin] if _admin_origin != "*" else ["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ----------------------- Concurrency Limiter & Stats -----------------------


class ConcurrencyLimiter:
    """
    Async semaphore with observability.
    Acquire before calling upstream Hy3; release after the call completes
    (including streaming responses — release in the generator's finally block).
    """

    def __init__(self, max_concurrent: int, queue_timeout: float):
        self.max_concurrent = max_concurrent
        self.queue_timeout = queue_timeout
        self._sem = asyncio.Semaphore(max_concurrent)
        # Stats counters (all best-effort; not strictly atomic but fine for observability)
        self.active = 0
        self.total_acquired = 0
        self.total_rejected = 0
        self.total_completed = 0
        self.total_errors = 0
        self.peak_active = 0
        self.started_at = time.time()

    async def acquire(self) -> bool:
        """
        Try to acquire a slot within queue_timeout. Returns True on success.
        Note: asyncio.wait_for(sem.acquire(), timeout=0) has a race condition
        where the timer can fire before the semaphore's acquire runs, causing
        spurious failures even when slots are available. We work around this
        by using a small minimum timeout (1ms) for the non-blocking path.
        """
        # If non-blocking mode requested, use 1ms minimum to let the event loop
        # resolve racing acquires. This is fast enough to feel instant while
        # avoiding the spurious-timeout race.
        timeout = self.queue_timeout if self.queue_timeout > 0 else 0.001
        try:
            await asyncio.wait_for(self._sem.acquire(), timeout=timeout)
            self.active += 1
            self.total_acquired += 1
            if self.active > self.peak_active:
                self.peak_active = self.active
            return True
        except asyncio.TimeoutError:
            self.total_rejected += 1
            return False

    def release(self, *, errored: bool = False) -> None:
        self.active -= 1
        self.total_completed += 1
        if errored:
            self.total_errors += 1
        self._sem.release()

    def stats(self) -> dict:
        return {
            "max_concurrent": self.max_concurrent,
            "queue_timeout_seconds": self.queue_timeout,
            "active_requests": self.active,
            "peak_active": self.peak_active,
            "available_slots": max(0, self.max_concurrent - self.active),
            "total_acquired": self.total_acquired,
            "total_rejected_503": self.total_rejected,
            "total_completed": self.total_completed,
            "total_errors": self.total_errors,
            "uptime_seconds": round(time.time() - self.started_at, 1),
        }


limiter = ConcurrencyLimiter(MAX_CONCURRENT, QUEUE_TIMEOUT)


# ----------------------- Pydantic Models -----------------------


class ChatMessage(BaseModel):
    role: str
    content: Optional[str] = None
    name: Optional[str] = None
    tool_calls: Optional[list] = None
    tool_call_id: Optional[str] = None
    reasoning_content: Optional[str] = None


class ChatCompletionRequest(BaseModel):
    model: str = DEFAULT_MODEL
    messages: list[ChatMessage]
    temperature: Optional[float] = None
    top_p: Optional[float] = None
    max_tokens: Optional[int] = DEFAULT_MAX_TOKENS
    stream: bool = False
    tools: Optional[list] = None
    tool_choice: Optional[Any] = None
    # Hy3-specific extras (pass via `extra_body` in OpenAI client)
    think_level: Optional[str] = DEFAULT_THINK_LEVEL
    stop: Optional[Any] = None
    n: Optional[int] = 1
    user: Optional[str] = None
    presence_penalty: Optional[float] = None
    frequency_penalty: Optional[float] = None
    logit_bias: Optional[dict] = None
    seed: Optional[int] = None


# ----------------------- Helpers -----------------------


def messages_to_hy3(messages: list[ChatMessage]) -> tuple[str, str, list]:
    """
    Convert OpenAI messages list to Hy3 (msg, system_prompt, history) tuple.
    Hy3 history format (confirmed via runtime inspection): a flat list of
    {"role": "user"|"assistant", "content": str} objects — same shape OpenAI uses.
    The latest user message becomes the top-level `msg` parameter; all prior
    non-system messages become the `history` list.
    """
    system_prompt = ""
    history: list = []
    last_user_msg = ""

    # Use the last system message as the system prompt
    non_system: list[ChatMessage] = []
    for m in messages:
        if m.role == "system":
            system_prompt = m.content or ""
        else:
            non_system.append(m)

    if non_system:
        # The final message is treated as the current prompt; everything before
        # it is the conversation history.
        last = non_system[-1]
        last_user_msg = last.content or ""
        prior = non_system[:-1]

        for m in prior:
            entry: dict = {"role": m.role, "content": m.content or ""}
            # Preserve assistant tool_calls in history so the model retains context
            if m.role == "assistant" and m.tool_calls:
                entry["tool_calls"] = m.tool_calls
            if m.role == "tool" and m.tool_call_id:
                entry["tool_call_id"] = m.tool_call_id
            history.append(entry)

    return last_user_msg, system_prompt, history


def build_payload(
    msg: str,
    system_prompt: str,
    history: list,
    think_level: str,
    max_tokens: int,
    temperature: Optional[float],
    top_p: Optional[float],
    tools: Optional[list],
) -> dict:
    return {
        "data": [
            msg,
            system_prompt,
            history,
            think_level,
            temperature,  # null or float
            max_tokens,
            top_p,  # null or float
            PRESERVED_THINKING,
            json.dumps(tools or [], ensure_ascii=False),
        ]
    }


async def call_hy3_stream(
    payload: dict,
    request_id: Optional[str] = None,
    record: Optional[RequestRecord] = None,
) -> AsyncIterator[list]:
    """
    POST to Hy3 to get event_id, then GET the SSE stream.
    Yields parsed SSE data payloads (the JSON list inside `data:`).
    Instrumented with detailed logging at every step.
    """
    t_post_start = time.perf_counter()
    log_event(
        "info",
        "upstream.post.start",
        request_id=request_id,
        url=HY3_BASE,
        payload_size_bytes=len(json.dumps(payload, default=str)),
        msg_preview=(payload.get("data", [""])[0] or "")[:80],
        history_len=len(payload.get("data", [""] * 4)[2] if len(payload.get("data", [])) > 2 else []),
        tools_count=len(json.loads(payload.get("data", [""] * 9)[8]) if len(payload.get("data", [])) > 8 and isinstance(payload.get("data", [])[8], str) else "[]"),
    )

    async with httpx.AsyncClient(timeout=HTTP_TIMEOUT) as client:
        # Step 1: get event_id
        try:
            r = await client.post(HY3_BASE, json=payload)
        except httpx.TimeoutException as e:
            log_event(
                "error",
                "upstream.post.timeout",
                request_id=request_id,
                error=f"{type(e).__name__}: {e}",
                elapsed_ms=round((time.perf_counter() - t_post_start) * 1000, 1),
            )
            if record:
                record.error = f"upstream POST timeout: {e}"
            raise HTTPException(status_code=504, detail=f"Hy3 POST timeout: {e}")
        except httpx.HTTPError as e:
            log_event(
                "error",
                "upstream.post.error",
                request_id=request_id,
                error=f"{type(e).__name__}: {e}",
                elapsed_ms=round((time.perf_counter() - t_post_start) * 1000, 1),
            )
            if record:
                record.error = f"upstream POST error: {e}"
            raise HTTPException(status_code=502, detail=f"Hy3 POST failed: {e}")

        post_latency_ms = round((time.perf_counter() - t_post_start) * 1000, 1)
        if record:
            record.upstream_post_status = r.status_code
            record.upstream_post_latency_ms = post_latency_ms

        if r.status_code != 200:
            body_preview = r.text[:300]
            log_event(
                "error",
                "upstream.post.non_200",
                request_id=request_id,
                status=r.status_code,
                body=body_preview,
                elapsed_ms=post_latency_ms,
            )
            if record:
                record.error = f"upstream POST {r.status_code}: {body_preview}"
            raise HTTPException(
                status_code=502,
                detail=f"Hy3 POST failed ({r.status_code}): {body_preview}",
            )

        try:
            event_id = r.json().get("event_id")
        except Exception as e:
            log_event(
                "error",
                "upstream.post.bad_json",
                request_id=request_id,
                error=str(e),
                body=r.text[:300],
            )
            if record:
                record.error = f"upstream POST bad JSON: {e}"
            raise HTTPException(status_code=502, detail=f"Hy3 POST returned bad JSON: {e}")

        if not event_id:
            log_event(
                "error",
                "upstream.post.no_event_id",
                request_id=request_id,
                body=r.text[:300],
            )
            if record:
                record.error = "upstream POST returned no event_id"
            raise HTTPException(status_code=502, detail="Hy3 returned no event_id")

        if record:
            record.upstream_event_id = event_id
        log_event(
            "info",
            "upstream.post.ok",
            request_id=request_id,
            event_id=event_id,
            status=r.status_code,
            elapsed_ms=post_latency_ms,
        )

        # Step 2: stream SSE
        t_stream_start = time.perf_counter()
        log_event(
            "info",
            "upstream.stream.start",
            request_id=request_id,
            url=f"{HY3_BASE}/{event_id}",
        )

        try:
            async with client.stream("GET", f"{HY3_BASE}/{event_id}") as resp:
                if resp.status_code != 200:
                    body = await resp.aread()
                    body_str = body.decode(errors="replace")[:300]
                    log_event(
                        "error",
                        "upstream.stream.non_200",
                        request_id=request_id,
                        status=resp.status_code,
                        body=body_str,
                    )
                    if record:
                        record.error = f"upstream stream GET {resp.status_code}: {body_str}"
                    raise HTTPException(
                        status_code=502,
                        detail=f"Hy3 stream GET failed ({resp.status_code}): {body_str}",
                    )
                chunk_count = 0
                buffer = ""
                first_chunk_latency_ms: Optional[float] = None
                async for chunk in resp.aiter_text():
                    buffer += chunk
                    while "\n" in buffer:
                        line, buffer = buffer.split("\n", 1)
                        line = line.strip()
                        if not line.startswith("data: "):
                            continue
                        payload_str = line[6:]
                        if payload_str in ("", "null", "[DONE]"):
                            continue
                        try:
                            data = json.loads(payload_str)
                        except json.JSONDecodeError as e:
                            log_event(
                                "warning",
                                "upstream.stream.bad_json",
                                request_id=request_id,
                                error=str(e),
                                line=payload_str[:200],
                            )
                            continue
                        chunk_count += 1
                        if first_chunk_latency_ms is None:
                            first_chunk_latency_ms = round(
                                (time.perf_counter() - t_stream_start) * 1000, 1
                            )
                            log_event(
                                "info",
                                "upstream.stream.first_chunk",
                                request_id=request_id,
                                chunk_index=chunk_count,
                                ttfb_ms=first_chunk_latency_ms,
                            )
                        yield data

                stream_total_ms = round((time.perf_counter() - t_stream_start) * 1000, 1)
                if record:
                    record.upstream_stream_latency_ms = stream_total_ms
                    record.upstream_chunks = chunk_count
                log_event(
                    "info",
                    "upstream.stream.done",
                    request_id=request_id,
                    chunks=chunk_count,
                    elapsed_ms=stream_total_ms,
                )
        except httpx.TimeoutException as e:
            log_event(
                "error",
                "upstream.stream.timeout",
                request_id=request_id,
                error=f"{type(e).__name__}: {e}",
                elapsed_ms=round((time.perf_counter() - t_stream_start) * 1000, 1),
            )
            if record:
                record.error = f"upstream stream timeout: {e}"
            raise HTTPException(status_code=504, detail=f"Hy3 stream timeout: {e}")
        except httpx.HTTPError as e:
            log_event(
                "error",
                "upstream.stream.error",
                request_id=request_id,
                error=f"{type(e).__name__}: {e}",
                elapsed_ms=round((time.perf_counter() - t_stream_start) * 1000, 1),
            )
            if record:
                record.error = f"upstream stream error: {e}"
            raise HTTPException(status_code=502, detail=f"Hy3 stream failed: {e}")


def parse_hy3_data(data: list) -> tuple[str, str, list, list]:
    """
    Extract (response_text, thinking_text, tool_calls, history) from a Hy3 SSE payload.
    Hy3 payload shape: [[response_text, thinking_text, tool_calls, history]]
    """
    if not data or not isinstance(data, list):
        return "", "", [], []
    inner = data[0]
    if not isinstance(inner, list) or not inner:
        return "", "", [], []
    resp = inner[0] if len(inner) > 0 else ""
    think = inner[1] if len(inner) > 1 and inner[1] else ""
    tools = inner[2] if len(inner) > 2 and inner[2] else []
    hist = inner[3] if len(inner) > 3 and inner[3] else []
    return resp or "", think or "", tools or [], hist or []


def make_chunk(
    completion_id: str,
    model: str,
    *,
    content: str = "",
    reasoning: str = "",
    role: Optional[str] = None,
    tool_calls: Optional[list] = None,
    finish_reason: Optional[str] = None,
) -> dict:
    delta: dict = {}
    if role:
        delta["role"] = role
    if content:
        delta["content"] = content
    if reasoning:
        delta["reasoning_content"] = reasoning
    if tool_calls:
        delta["tool_calls"] = tool_calls
    return {
        "id": completion_id,
        "object": "chat.completion.chunk",
        "created": int(time.time()),
        "model": model,
        "choices": [
            {
                "index": 0,
                "delta": delta,
                "finish_reason": finish_reason,
            }
        ],
    }


def make_tool_call_objects(tool_calls: list) -> list:
    """Convert Hy3 tool_calls to OpenAI message.tool_calls format."""
    out = []
    for tc in tool_calls:
        fn = tc.get("function", {}) if isinstance(tc, dict) else {}
        args = fn.get("arguments", "{}")
        if not isinstance(args, str):
            args = json.dumps(args, ensure_ascii=False)
        out.append(
            {
                "id": tc.get("id", f"call_{uuid.uuid4().hex[:24]}") if isinstance(tc, dict) else f"call_{uuid.uuid4().hex[:24]}",
                "type": "function",
                "function": {
                    "name": fn.get("name", "unknown"),
                    "arguments": args,
                },
            }
        )
    return out


def make_tool_call_delta(tool_calls: list) -> list:
    """Convert Hy3 tool_calls to OpenAI streaming delta format."""
    out = []
    for i, tc in enumerate(tool_calls):
        fn = tc.get("function", {}) if isinstance(tc, dict) else {}
        args = fn.get("arguments", "{}")
        if not isinstance(args, str):
            args = json.dumps(args, ensure_ascii=False)
        out.append(
            {
                "index": i,
                "id": tc.get("id", f"call_{uuid.uuid4().hex[:24]}") if isinstance(tc, dict) else f"call_{uuid.uuid4().hex[:24]}",
                "type": "function",
                "function": {
                    "name": fn.get("name", "unknown"),
                    "arguments": args,
                },
            }
        )
    return out


# ----------------------- Endpoints -----------------------


@app.get("/")
async def root():
    return {
        "service": "Hy3 OpenAI-Compatible API",
        "version": "1.2.0",
        "endpoints": [
            "/",
            "/health",
            "/stats",
            "/v1/models",
            "/v1/chat/completions",
            "/admin/logs",
            "/admin/requests",
            "/admin/logs/summary",
        ],
        "models": ["hy3", "hy3-think"],
        "usage": "Point your OpenAI client to http://<host>:<port>/v1 with any API key",
        "limits": {"max_concurrent": MAX_CONCURRENT, "queue_timeout": QUEUE_TIMEOUT},
    }


@app.get("/health")
async def health():
    """Liveness/readiness probe for container orchestrators (Render, Fly, K8s)."""
    s = limiter.stats()
    healthy = s["active_requests"] <= s["max_concurrent"]
    return JSONResponse(
        status_code=200 if healthy else 503,
        content={"status": "ok" if healthy else "overloaded", **s},
    )


@app.get("/stats")
async def stats():
    """Runtime stats: active/peak/rejected request counters."""
    return limiter.stats()


@app.get("/v1/models")
async def list_models():
    now = int(time.time())
    return {
        "object": "list",
        "data": [
            {
                "id": "hy3",
                "object": "model",
                "created": now,
                "owned_by": "tencent",
            },
            {
                "id": "hy3-think",
                "object": "model",
                "created": now,
                "owned_by": "tencent",
            },
        ],
    }


@app.post("/v1/chat/completions")
async def chat_completions(req: ChatCompletionRequest, request: Request):
    # Per-request ID + record for tracing through the entire pipeline
    request_id = new_request_id()
    client_ip = request.client.host if request.client else "unknown"
    record = RequestRecord(
        request_id=request_id,
        method="POST",
        path="/v1/chat/completions",
        client_ip=client_ip,
    )
    record.model = req.model
    record.stream = req.stream
    record.has_tools = bool(req.tools)
    record.message_count = len(req.messages)
    record.think_level = req.think_level or DEFAULT_THINK_LEVEL
    # Find last user message for preview
    for m in reversed(req.messages):
        if m.role == "user" and m.content:
            record.user_message_preview = m.content[:100]
            break
    record.has_history = len(req.messages) > 1

    log_event(
        "info",
        "request.start",
        request_id=request_id,
        method="POST",
        path="/v1/chat/completions",
        client_ip=client_ip,
        model=req.model,
        stream=req.stream,
        has_tools=record.has_tools,
        message_count=record.message_count,
        think_level=record.think_level,
        user_agent=request.headers.get("user-agent", "")[:100],
        user_msg_preview=record.user_message_preview,
    )

    # Map "hy3-think" model to think_level=high (unless user explicitly set think_level)
    think_level = req.think_level or DEFAULT_THINK_LEVEL
    if req.model == "hy3-think" and think_level == DEFAULT_THINK_LEVEL:
        think_level = "high"
        record.think_level = "high"

    msg, sys_prompt, history = messages_to_hy3(req.messages)
    if not msg and not (req.messages and req.messages[-1].content):
        log_event(
            "warning",
            "request.bad_request",
            request_id=request_id,
            error="No user message provided",
        )
        record.status_code = 400
        record.error = "No user message provided"
        record.finalize()
        raise HTTPException(status_code=400, detail="No user message provided")

    payload = build_payload(
        msg=msg,
        system_prompt=sys_prompt,
        history=history,
        think_level=think_level,
        max_tokens=req.max_tokens or DEFAULT_MAX_TOKENS,
        temperature=req.temperature,
        top_p=req.top_p,
        tools=req.tools,
    )

    completion_id = f"chatcmpl-{uuid.uuid4().hex[:24]}"

    # ----- Concurrency cap: acquire a slot before touching upstream Hy3 -----
    t_queue_start = time.perf_counter()
    record.concurrency_active_at_start = limiter.active
    acquired = await limiter.acquire()
    record.queued_ms = round((time.perf_counter() - t_queue_start) * 1000, 1)

    if not acquired:
        # At capacity and queue_timeout expired — fail fast with 503 + Retry-After
        retry_after = max(1, int(QUEUE_TIMEOUT))
        log_event(
            "warning",
            "request.rejected_503",
            request_id=request_id,
            reason="server_at_capacity",
            max_concurrent=MAX_CONCURRENT,
            active=limiter.active,
            queued_ms=record.queued_ms,
        )
        record.status_code = 503
        record.error = "Server at capacity"
        record.finalize()
        return JSONResponse(
            status_code=503,
            content={
                "error": {
                    "message": (
                        f"Server at capacity ({MAX_CONCURRENT} concurrent requests). "
                        f"Retry after {retry_after}s."
                    ),
                    "type": "server_at_capacity",
                    "code": "concurrency_limit",
                }
            },
            headers={
                "Retry-After": str(retry_after),
                "X-Max-Concurrent": str(MAX_CONCURRENT),
                "X-Active-Requests": str(limiter.active),
                "X-Request-Id": request_id,
            },
        )

    log_event(
        "info",
        "request.slot_acquired",
        request_id=request_id,
        queued_ms=record.queued_ms,
        active=limiter.active,
        peak=limiter.peak_active,
    )

    if req.stream:
        # Streaming path: release the slot when the generator finishes (or aborts)
        return StreamingResponse(
            stream_openai(
                completion_id,
                req.model,
                payload,
                limiter=limiter,
                request_id=request_id,
                record=record,
            ),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "X-Accel-Buffering": "no",
                "Connection": "keep-alive",
                "X-Max-Concurrent": str(MAX_CONCURRENT),
                "X-Request-Id": request_id,
            },
        )

    # Non-streaming: collect full response; release the slot in finally
    final_resp = ""
    final_think = ""
    final_tools: list = []
    errored = False
    try:
        async for data in call_hy3_stream(payload, request_id=request_id, record=record):
            resp, think, tools, _ = parse_hy3_data(data)
            if resp:
                final_resp = resp
            if think:
                final_think = think
            if tools:
                final_tools = tools
    except HTTPException as e:
        errored = True
        record.status_code = e.status_code
        record.error = str(e.detail)[:300]
        log_event(
            "error",
            "request.upstream_http_error",
            request_id=request_id,
            status=e.status_code,
            error=str(e.detail)[:300],
        )
        raise
    except Exception as e:
        errored = True
        record.status_code = 502
        record.error = str(e)[:300]
        log_event(
            "error",
            "request.unhandled_error",
            request_id=request_id,
            error=f"{type(e).__name__}: {e}",
        )
        raise HTTPException(status_code=502, detail=f"Hy3 call failed: {e}")
    finally:
        limiter.release(errored=errored)

    record.response_chars = len(final_resp)
    record.response_thinking_chars = len(final_think)
    record.response_tool_calls = len(final_tools)
    record.finish_reason = "tool_calls" if final_tools else "stop"
    record.status_code = 200
    record.finalize()

    log_event(
        "info",
        "request.done",
        request_id=request_id,
        status=200,
        duration_ms=record.to_dict()["duration_ms"],
        response_chars=record.response_chars,
        thinking_chars=record.response_thinking_chars,
        tool_calls=record.response_tool_calls,
        finish_reason=record.finish_reason,
    )

    message: dict = {"role": "assistant", "content": final_resp}
    if final_think:
        message["reasoning_content"] = final_think
    if final_tools:
        message["tool_calls"] = make_tool_call_objects(final_tools)

    finish_reason = "tool_calls" if final_tools else "stop"

    return {
        "id": completion_id,
        "object": "chat.completion",
        "created": int(time.time()),
        "model": req.model,
        "choices": [
            {
                "index": 0,
                "message": message,
                "finish_reason": finish_reason,
            }
        ],
        "usage": {
            "prompt_tokens": 0,
            "completion_tokens": 0,
            "total_tokens": 0,
        },
    }


async def stream_openai(
    completion_id: str,
    model: str,
    payload: dict,
    limiter: Optional[ConcurrencyLimiter] = None,
    request_id: Optional[str] = None,
    record: Optional[RequestRecord] = None,
):
    """Yield OpenAI-format SSE chunks. Releases the limiter slot in finally."""
    last_resp_len = 0
    last_think_len = 0
    final_tools: Optional[list] = None
    errored = False

    log_event(
        "info",
        "stream.start",
        request_id=request_id,
        completion_id=completion_id,
        model=model,
    )

    # Initial role chunk
    yield f"data: {json.dumps(make_chunk(completion_id, model, role='assistant'))}\n\n"

    try:
        async for data in call_hy3_stream(payload, request_id=request_id, record=record):
            resp, think, tools, _ = parse_hy3_data(data)

            # Emit thinking deltas (as reasoning_content, OpenAI o1-style)
            if think and len(think) > last_think_len:
                chunk = make_chunk(
                    completion_id, model, reasoning=think[last_think_len:]
                )
                yield f"data: {json.dumps(chunk, ensure_ascii=False)}\n\n"
                last_think_len = len(think)
                if record:
                    record.response_thinking_chars = len(think)

            # Emit response deltas
            if resp and len(resp) > last_resp_len:
                chunk = make_chunk(
                    completion_id, model, content=resp[last_resp_len:]
                )
                yield f"data: {json.dumps(chunk, ensure_ascii=False)}\n\n"
                last_resp_len = len(resp)
                if record:
                    record.response_chars = len(resp)

            if tools:
                final_tools = tools

        # Emit tool calls at the end if any
        if final_tools:
            tc_delta = make_tool_call_delta(final_tools)
            yield f"data: {json.dumps(make_chunk(completion_id, model, tool_calls=tc_delta), ensure_ascii=False)}\n\n"
            yield f"data: {json.dumps(make_chunk(completion_id, model, finish_reason='tool_calls'))}\n\n"
            if record:
                record.response_tool_calls = len(final_tools)
                record.finish_reason = "tool_calls"
        else:
            yield f"data: {json.dumps(make_chunk(completion_id, model, finish_reason='stop'))}\n\n"
            if record:
                record.finish_reason = "stop"

        yield "data: [DONE]\n\n"
        if record:
            record.status_code = 200
            record.finalize()
        log_event(
            "info",
            "stream.done",
            request_id=request_id,
            response_chars=last_resp_len,
            thinking_chars=last_think_len,
            tool_calls=len(final_tools or []),
            finish_reason=record.finish_reason if record else None,
        )
    except httpx.HTTPStatusError as e:
        errored = True
        err = {
            "error": {
                "message": f"Hy3 upstream error: {e.response.status_code} {e.response.text[:200]}",
                "type": "upstream_error",
            }
        }
        yield f"data: {json.dumps(err)}\n\n"
        yield "data: [DONE]\n\n"
        if record:
            record.status_code = 502
            record.error = f"upstream HTTP {e.response.status_code}"
            record.finalize()
        log_event("error", "stream.upstream_error", request_id=request_id, error=str(e)[:200])
    except HTTPException as e:
        errored = True
        err = {"error": {"message": str(e.detail), "type": "upstream_error"}}
        yield f"data: {json.dumps(err)}\n\n"
        yield "data: [DONE]\n\n"
        if record:
            record.status_code = e.status_code
            record.error = str(e.detail)[:300]
            record.finalize()
        log_event("error", "stream.http_error", request_id=request_id, status=e.status_code, error=str(e.detail)[:200])
    except Exception as e:
        errored = True
        err = {"error": {"message": str(e), "type": "internal_error"}}
        yield f"data: {json.dumps(err)}\n\n"
        yield "data: [DONE]\n\n"
        if record:
            record.status_code = 500
            record.error = f"{type(e).__name__}: {e}"
            record.finalize()
        log_event("error", "stream.unhandled_error", request_id=request_id, error=f"{type(e).__name__}: {e}")
    finally:
        # ALWAYS release the limiter slot — even on client disconnect.
        # Without this, an aborted stream would leak the slot forever.
        if limiter is not None:
            limiter.release(errored=errored)


# ----------------------- Admin Endpoints -----------------------


@app.get("/admin/logs")
async def admin_logs(
    limit: int = 100,
    level: Optional[str] = None,
    request_id: Optional[str] = None,
    event: Optional[str] = None,
):
    """
    Return recent log entries from the in-memory ring buffer.
    Filters: level (debug/info/warning/error), request_id, event name.
    Newest first.
    """
    return {
        "total": len(get_recent_logs(limit=10000)),
        "returned": len(
            get_recent_logs(limit=limit, level=level, request_id=request_id, event=event)
        ),
        "filters": {"level": level, "request_id": request_id, "event": event, "limit": limit},
        "logs": get_recent_logs(
            limit=limit, level=level, request_id=request_id, event=event
        ),
    }


@app.get("/admin/requests")
async def admin_requests(limit: int = 50, errors_only: bool = False):
    """Return recent request records (newest first)."""
    return {
        "total_tracked": len(get_recent_requests(limit=10000)),
        "returned": len(get_recent_requests(limit=limit, errors_only=errors_only)),
        "filters": {"limit": limit, "errors_only": errors_only},
        "requests": get_recent_requests(limit=limit, errors_only=errors_only),
    }


@app.get("/admin/logs/summary")
async def admin_logs_summary():
    """Counts by level + event — for dashboard widgets."""
    return get_log_summary()


@app.get("/admin/requests/{request_id}")
async def admin_request_detail(request_id: str):
    """Get a single request record + all log entries for it."""
    requests = [r for r in get_recent_requests(limit=10000) if r["request_id"] == request_id]
    logs = get_recent_logs(limit=500, request_id=request_id)
    if not requests and not logs:
        raise HTTPException(status_code=404, detail=f"Request {request_id} not found")
    return {
        "request": requests[0] if requests else None,
        "logs": logs,
    }


# ----------------------- Entrypoint -----------------------


if __name__ == "__main__":
    import os
    import uvicorn

    parser = argparse.ArgumentParser(description="Hy3 OpenAI-Compatible API Server")
    parser.add_argument(
        "--host",
        default=os.environ.get("HOST", "0.0.0.0"),
        help="Bind host (default: 0.0.0.0 or $HOST)",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=int(os.environ.get("PORT", "8000")),
        help="Bind port (default: 8000 or $PORT)",
    )
    parser.add_argument("--reload", action="store_true", help="Enable auto-reload")
    args = parser.parse_args()

    uvicorn.run(
        "server:app" if args.reload else app,
        host=args.host,
        port=args.port,
        reload=args.reload,
        log_level="info",
    )
