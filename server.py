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
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel

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
    version="1.1.0",
    description="OpenAI-compatible proxy for Tencent Hy3 295B MoE via HuggingFace Gradio API.",
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


async def call_hy3_stream(payload: dict) -> AsyncIterator[list]:
    """
    POST to Hy3 to get event_id, then GET the SSE stream.
    Yields parsed SSE data payloads (the JSON list inside `data:`).
    """
    async with httpx.AsyncClient(timeout=HTTP_TIMEOUT) as client:
        # Step 1: get event_id
        r = await client.post(HY3_BASE, json=payload)
        if r.status_code != 200:
            raise HTTPException(
                status_code=502,
                detail=f"Hy3 POST failed ({r.status_code}): {r.text[:200]}",
            )
        event_id = r.json().get("event_id")
        if not event_id:
            raise HTTPException(status_code=502, detail="Hy3 returned no event_id")

        # Step 2: stream SSE
        async with client.stream("GET", f"{HY3_BASE}/{event_id}") as resp:
            if resp.status_code != 200:
                body = await resp.aread()
                raise HTTPException(
                    status_code=502,
                    detail=f"Hy3 stream GET failed ({resp.status_code}): {body[:200]}",
                )
            buffer = ""
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
                        yield json.loads(payload_str)
                    except json.JSONDecodeError:
                        continue


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
        "version": "1.1.0",
        "endpoints": ["/", "/health", "/stats", "/v1/models", "/v1/chat/completions"],
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
async def chat_completions(req: ChatCompletionRequest):
    # Map "hy3-think" model to think_level=high (unless user explicitly set think_level)
    think_level = req.think_level or DEFAULT_THINK_LEVEL
    if req.model == "hy3-think" and think_level == DEFAULT_THINK_LEVEL:
        think_level = "high"

    msg, sys_prompt, history = messages_to_hy3(req.messages)
    if not msg and not (req.messages and req.messages[-1].content):
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
    acquired = await limiter.acquire()
    if not acquired:
        # At capacity and queue_timeout expired — fail fast with 503 + Retry-After
        retry_after = max(1, int(QUEUE_TIMEOUT))
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
            },
        )

    if req.stream:
        # Streaming path: release the slot when the generator finishes (or aborts)
        return StreamingResponse(
            stream_openai(completion_id, req.model, payload, limiter=limiter),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "X-Accel-Buffering": "no",
                "Connection": "keep-alive",
                "X-Max-Concurrent": str(MAX_CONCURRENT),
            },
        )

    # Non-streaming: collect full response; release the slot in finally
    final_resp = ""
    final_think = ""
    final_tools: list = []
    errored = False
    try:
        async for data in call_hy3_stream(payload):
            resp, think, tools, _ = parse_hy3_data(data)
            if resp:
                final_resp = resp
            if think:
                final_think = think
            if tools:
                final_tools = tools
    except HTTPException as e:
        errored = True
        raise
    except Exception as e:
        errored = True
        raise HTTPException(status_code=502, detail=f"Hy3 call failed: {e}")
    finally:
        limiter.release(errored=errored)

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
):
    """Yield OpenAI-format SSE chunks. Releases the limiter slot in finally."""
    last_resp_len = 0
    last_think_len = 0
    final_tools: Optional[list] = None
    errored = False

    # Initial role chunk
    yield f"data: {json.dumps(make_chunk(completion_id, model, role='assistant'))}\n\n"

    try:
        async for data in call_hy3_stream(payload):
            resp, think, tools, _ = parse_hy3_data(data)

            # Emit thinking deltas (as reasoning_content, OpenAI o1-style)
            if think and len(think) > last_think_len:
                chunk = make_chunk(
                    completion_id, model, reasoning=think[last_think_len:]
                )
                yield f"data: {json.dumps(chunk, ensure_ascii=False)}\n\n"
                last_think_len = len(think)

            # Emit response deltas
            if resp and len(resp) > last_resp_len:
                chunk = make_chunk(
                    completion_id, model, content=resp[last_resp_len:]
                )
                yield f"data: {json.dumps(chunk, ensure_ascii=False)}\n\n"
                last_resp_len = len(resp)

            if tools:
                final_tools = tools

        # Emit tool calls at the end if any
        if final_tools:
            tc_delta = make_tool_call_delta(final_tools)
            yield f"data: {json.dumps(make_chunk(completion_id, model, tool_calls=tc_delta), ensure_ascii=False)}\n\n"
            yield f"data: {json.dumps(make_chunk(completion_id, model, finish_reason='tool_calls'))}\n\n"
        else:
            yield f"data: {json.dumps(make_chunk(completion_id, model, finish_reason='stop'))}\n\n"

        yield "data: [DONE]\n\n"
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
    except HTTPException as e:
        errored = True
        err = {"error": {"message": str(e.detail), "type": "upstream_error"}}
        yield f"data: {json.dumps(err)}\n\n"
        yield "data: [DONE]\n\n"
    except Exception as e:
        errored = True
        err = {"error": {"message": str(e), "type": "internal_error"}}
        yield f"data: {json.dumps(err)}\n\n"
        yield "data: [DONE]\n\n"
    finally:
        # ALWAYS release the limiter slot — even on client disconnect.
        # Without this, an aborted stream would leak the slot forever.
        if limiter is not None:
            limiter.release(errored=errored)


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
