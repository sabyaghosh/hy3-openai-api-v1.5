"""
OpenAI-compatible API server for Tencent Hy3 (295B MoE) via HuggingFace Gradio API.
No API key required. Drop-in replacement for OpenAI base_url.

Run:
    pip install "fastapi>=0.110" "uvicorn[standard]>=0.27" "httpx>=0.27" "pydantic>=2.5"
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
import hmac
import json
import os
import time
import uuid
from typing import Any, AsyncIterator, Optional, Union

import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, field_validator

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
# Whether to preserve thinking content in the Hy3 response. Passed as the 8th
# field in the Gradio data payload. When True, Hy3 includes reasoning text in
# the response so we can expose it as reasoning_content (OpenAI o1-style).
PRESERVED_THINKING = True
# Read timeout (90s) is below Render's ~100s gateway timeout so the upstream
# call fails before the gateway returns 504 to the client (which would leave the
# limiter slot occupied for the remaining ~210s of the old 300s timeout).
HTTP_TIMEOUT = httpx.Timeout(90.0, connect=30.0)

# Single source of truth for the version string. Used by both the FastAPI app
# metadata (visible at /docs, /openapi.json) and the / endpoint.
__version__ = "1.4.0"

# ----------------------- Env var parsing -----------------------

def _parse_int_env(name: str, default: int, min_val: int = 1) -> int:
    """Parse an int env var with validation; fall back to default on bad input."""
    raw = os.environ.get(name, str(default))
    try:
        val = int(raw)
    except (ValueError, TypeError):
        print(f"Warning: {name}={raw!r} is not a valid int, using {default}", flush=True)
        return default
    if val < min_val:
        print(f"Warning: {name}={val} is below minimum {min_val}, using {min_val}", flush=True)
        return min_val
    return val


def _parse_float_env(name: str, default: float, min_val: float = 0.0) -> float:
    raw = os.environ.get(name, str(default))
    try:
        val = float(raw)
    except (ValueError, TypeError):
        print(f"Warning: {name}={raw!r} is not a valid float, using {default}", flush=True)
        return default
    if val < min_val:
        print(f"Warning: {name}={val} is below minimum {min_val}, using {min_val}", flush=True)
        return min_val
    return val


# Concurrency control — prevents upstream Hy3 overload and gateway timeouts.
# Tune via env vars:
#   MAX_CONCURRENT  — hard cap of in-flight Hy3 calls (default 10)
#   QUEUE_TIMEOUT   — seconds to wait for a slot before returning 503 (default 5.0)
#                     Set to 0 for non-blocking (immediate 503 when at capacity).
MAX_CONCURRENT = _parse_int_env("MAX_CONCURRENT", 10, min_val=1)
QUEUE_TIMEOUT = _parse_float_env("QUEUE_TIMEOUT", 5.0, min_val=0.0)

# ----------------------- Security config -----------------------
# Optional API key(s). Comma-separated. If empty, no auth required (open proxy).
#   export API_KEYS="key1,key2,key3"
_API_KEYS_RAW = os.environ.get("API_KEYS", "").strip()
API_KEYS: set[str] = {k.strip() for k in _API_KEYS_RAW.split(",") if k.strip()} if _API_KEYS_RAW else set()

# Optional admin token for /admin/* endpoints. If unset, admin endpoints return 404.
#   export ADMIN_TOKEN="my-secret-admin-token"
ADMIN_TOKEN = os.environ.get("ADMIN_TOKEN", "").strip()

# Input size limits (DoS protection)
MAX_MESSAGES = _parse_int_env("MAX_MESSAGES", 1000, min_val=1)
MAX_CONTENT_CHARS = _parse_int_env("MAX_CONTENT_CHARS", 1_000_000, min_val=1024)

app = FastAPI(
    title="Hy3 OpenAI-Compatible API",
    version=__version__,
    description="OpenAI-compatible proxy for Tencent Hy3 295B MoE via HuggingFace Gradio API.",
)

# CORS — allow the Next.js admin panel (and any OpenAI client) to call this API.
# Configure allowed origins via ADMIN_ORIGIN env var (default: permissive for dev).
# "*" + credentials=true is invalid per CORS spec; browsers block credentialed
# requests when origin is "*". Use two distinct configurations.
_admin_origin = os.environ.get("ADMIN_ORIGIN", "*")
if _admin_origin == "*":
    # Wildcard mode — no credentials (spec-compliant)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_credentials=False,
        allow_methods=["*"],
        allow_headers=["*"],
    )
else:
    # Specific origin — credentials allowed (spec-compliant)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=[_admin_origin],
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
        # Stats counters. Under single-threaded asyncio, increments/decrements
        # between await points are atomic from the event loop's perspective, so
        # these are safe without explicit locking. peak_active may briefly lag
        # under concurrent acquire() calls, but that's acceptable for observability.
        self.active = 0
        self.total_acquired = 0
        self.total_rejected = 0
        self.total_completed = 0
        self.total_errors = 0
        self.peak_active = 0
        self.started_at = time.time()

    async def acquire(self) -> bool:
        """
        Try to acquire a slot within queue_timeout. Returns True on success,
        False if the queue expired (caller should return 503).

        For non-blocking mode (queue_timeout <= 0), uses a 1ms timeout as an
        approximation of "immediate" — asyncio.Semaphore has no try-acquire that
        can distinguish "available now" from "became available in 1ms", but the
        1ms ceiling is negligible in practice. For normal mode, uses the
        configured queue_timeout.
        """
        timeout = self.queue_timeout if self.queue_timeout > 0 else 0.001
        try:
            await asyncio.wait_for(self._sem.acquire(), timeout=timeout)
        except asyncio.TimeoutError:
            self.total_rejected += 1
            return False
        self.active += 1
        self.total_acquired += 1
        if self.active > self.peak_active:
            self.peak_active = self.active
        return True

    def release(self, *, errored: bool = False) -> None:
        # Guard against active counter underflow on double-release. If active is
        # already 0, this release is spurious — log and return without touching
        # the semaphore (it's already at max capacity).
        if self.active <= 0:
            log_event("warning", "limiter.release_underflow", active=self.active)
            return
        self.active -= 1
        # An errored request should NOT count as 'completed'; track errors
        # separately so total_acquired == total_completed + total_errors.
        if errored:
            self.total_errors += 1
        else:
            self.total_completed += 1
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
    # Accept str OR list of parts (OpenAI multimodal format, e.g.
    # [{"type": "text", "text": "..."}, {"type": "image_url", "image_url": {...}}]).
    # When a list is passed, we extract text parts for the upstream Hy3 call.
    content: Optional[Union[str, list]] = None
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
    # think_level must be None (not DEFAULT_THINK_LEVEL) so we can distinguish
    # "user explicitly set no_think" from "user did not set it". The hy3-think
    # model only overrides when think_level is None.
    think_level: Optional[str] = None
    # The following fields are accepted for OpenAI spec compatibility but are
    # NOT forwarded to upstream Hy3 (it doesn't support them). They are silently
    # ignored. If Hy3 adds support in the future, wire them up in build_payload().
    stop: Optional[Any] = None
    n: Optional[int] = 1
    user: Optional[str] = None
    presence_penalty: Optional[float] = None
    frequency_penalty: Optional[float] = None
    logit_bias: Optional[dict] = None
    seed: Optional[int] = None

    # Input size limits to prevent DoS via huge payloads
    @field_validator("messages")
    @classmethod
    def _validate_messages_size(cls, v):
        if len(v) > MAX_MESSAGES:
            raise ValueError(f"Too many messages (max {MAX_MESSAGES})")
        total_chars = sum(len(_content_to_str(m.content)) for m in v)
        if total_chars > MAX_CONTENT_CHARS:
            raise ValueError(
                f"Total message content too large ({total_chars} chars, max {MAX_CONTENT_CHARS})"
            )
        return v

    @field_validator("think_level")
    @classmethod
    def _validate_think_level(cls, v):
        if v is None:
            return v
        allowed = {"high", "low", "no_think"}
        if v not in allowed:
            raise ValueError(f"think_level must be one of {allowed}, got {v!r}")
        return v

    @field_validator("n")
    @classmethod
    def _validate_n(cls, v):
        # Hy3 only supports n=1 (single completion). Reject n>1 explicitly so
        # clients get a clear error instead of silently receiving 1 choice.
        if v is not None and v != 1:
            raise ValueError(f"n must be 1 (Hy3 does not support multiple completions), got {v}")
        return v


# ----------------------- Helpers -----------------------


def _content_to_str(content: Optional[Union[str, list]]) -> str:
    """Convert OpenAI message content (str or list of parts) to a plain string.

    OpenAI's multimodal format passes content as a list of parts, e.g.:
      [{"type": "text", "text": "hello"}, {"type": "image_url", "image_url": {...}}]
    Hy3 only accepts a string, so we extract and concatenate all text parts.
    Non-text parts (images, audio) are silently dropped (Hy3 doesn't support them).
    """
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for p in content:
            if isinstance(p, dict) and p.get("type") == "text":
                parts.append(p.get("text", ""))
            elif isinstance(p, str):
                parts.append(p)
        return "\n".join(parts)
    return str(content)


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

    # Concatenate all system messages (OpenAI convention). Use _content_to_str
    # to handle multimodal list content.
    non_system: list[ChatMessage] = []
    system_parts: list[str] = []
    for m in messages:
        if m.role == "system":
            s = _content_to_str(m.content)
            if s:
                system_parts.append(s)
        else:
            non_system.append(m)
    system_prompt = "\n".join(system_parts)

    if non_system:
        # The final user message is treated as the current prompt; everything before
        # it becomes the conversation history. If the last message is not from the user
        # (e.g. assistant continuation), find the most recent user message.
        last = non_system[-1]
        if last.role == "user":
            last_user_msg = _content_to_str(last.content)
            prior = non_system[:-1]
        else:
            # Last message is not from user (e.g. assistant/tool). Find the MOST RECENT
            # user message by scanning in reverse; everything else goes to history.
            prior = list(non_system)  # shallow copy; we'll extract the prompt from it
            for i in range(len(prior) - 1, -1, -1):
                if prior[i].role == "user" and _content_to_str(prior[i].content).strip():
                    last_user_msg = _content_to_str(prior[i])
                    del prior[i]
                    break
            # If we never found a user message, use the last message as the prompt
            if not last_user_msg:
                last_user_msg = _content_to_str(last.content)
                prior = prior[:-1] if prior and prior[-1] is last else prior

        for m in prior:
            entry: dict = {"role": m.role, "content": _content_to_str(m.content)}
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
    # Extract payload fields once, then log — readable and avoids triple .get()
    payload_data = payload.get("data", [])
    msg_field = payload_data[0] if len(payload_data) > 0 else ""
    history_field = payload_data[2] if len(payload_data) > 2 else []
    tools_str = payload_data[8] if len(payload_data) > 8 else "[]"
    try:
        tools_count = len(json.loads(tools_str)) if isinstance(tools_str, str) else 0
    except (json.JSONDecodeError, TypeError):
        tools_count = 0
    log_event(
        "info",
        "upstream.post.start",
        request_id=request_id,
        url=HY3_BASE,
        payload_size_bytes=len(json.dumps(payload, default=str)),
        msg_preview=(msg_field or "")[:80],
        history_len=len(history_field) if isinstance(history_field, list) else 0,
        tools_count=tools_count,
    )

    # A new AsyncClient per request. For high-throughput deployments, a shared
    # client created in the FastAPI lifespan and stored on app.state would reduce
    # TCP/TLS overhead — but per-request is simpler and avoids lifespan complexity.
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
                            sse_data = json.loads(payload_str)
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
                        yield sse_data

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
    # `not inner` above guarantees len(inner) > 0, so inner[0] is safe.
    resp = inner[0] if inner[0] else ""
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
    created: Optional[int] = None,
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
        # Reuse the same created timestamp across all chunks in this completion
        # so clients see a consistent value (matches OpenAI behavior).
        "created": created if created is not None else int(time.time()),
        "model": model,
        "choices": [
            {
                "index": 0,
                "delta": delta,
                "finish_reason": finish_reason,
            }
        ],
    }


def _tool_call_to_openai(tc: dict, index: Optional[int] = None) -> dict:
    """Convert a single Hy3 tool_call to OpenAI format. Shared helper."""
    fn = tc.get("function", {}) if isinstance(tc, dict) else {}
    args = fn.get("arguments", "{}")
    if not isinstance(args, str):
        args = json.dumps(args, ensure_ascii=False)
    tc_id = tc.get("id") if isinstance(tc, dict) else None
    out: dict = {
        "id": tc_id or f"call_{uuid.uuid4().hex[:24]}",
        "type": "function",
        "function": {"name": fn.get("name", "unknown"), "arguments": args},
    }
    if index is not None:
        # Streaming delta format requires 'index' first
        return {"index": index, **out}
    return out


def make_tool_call_objects(tool_calls: list) -> list:
    """Convert Hy3 tool_calls to OpenAI message.tool_calls format."""
    return [_tool_call_to_openai(tc) for tc in tool_calls]


def make_tool_call_delta(tool_calls: list) -> list:
    """Convert Hy3 tool_calls to OpenAI streaming delta format."""
    return [_tool_call_to_openai(tc, index=i) for i, tc in enumerate(tool_calls)]


# ----------------------- Endpoints -----------------------


@app.get("/")
async def root():
    return {
        "service": "Hy3 OpenAI-Compatible API",
        "version": __version__,
        "endpoints": [
            "/",
            "/health",
            "/stats",
            "/v1/models",
            "/v1/chat/completions",
            "/admin/logs (requires ADMIN_TOKEN)",
            "/admin/requests (requires ADMIN_TOKEN)",
            "/admin/requests/{id} (requires ADMIN_TOKEN)",
            "/admin/logs/summary (requires ADMIN_TOKEN)",
        ],
        "models": ["hy3", "hy3-think"],
        "usage": "Point your OpenAI client to http://<host>:<port>/v1 with any API key",
        "limits": {
            "max_concurrent": MAX_CONCURRENT,
            "queue_timeout": QUEUE_TIMEOUT,
            "max_messages": MAX_MESSAGES,
            "max_content_chars": MAX_CONTENT_CHARS,
            "api_keys_required": bool(API_KEYS),
            "admin_token_required": bool(ADMIN_TOKEN),
        },
    }


@app.get("/health")
async def health():
    """Liveness/readiness probe for container orchestrators (Render, Fly, K8s).

    Returns 503 when the limiter reports more active requests than max_concurrent
    (which should never happen under normal operation — it indicates a counter bug
    or a slot leak). Also returns 503 when total_errors exceeds 50% of total_acquired
    with at least 10 acquired, indicating systemic upstream failures.
    """
    s = limiter.stats()
    overflow = s["active_requests"] > s["max_concurrent"]
    error_rate_high = (
        s["total_acquired"] >= 10
        and s["total_errors"] > s["total_acquired"] * 0.5
    )
    healthy = not overflow and not error_rate_high
    status = "ok" if healthy else ("overloaded" if overflow else "degraded")
    return JSONResponse(
        status_code=200 if healthy else 503,
        content={"status": status, **s},
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
    # Optional API key check
    if API_KEYS:
        auth = request.headers.get("authorization", "")
        token = auth.removeprefix("Bearer ").strip() if auth.startswith("Bearer ") else ""
        # Also allow raw API key in x-api-key header (some clients use this)
        if not token:
            token = request.headers.get("x-api-key", "").strip()
        # use constant-time comparison to prevent timing attacks. Compare
        # against each configured key with hmac.compare_digest so attackers cannot
        # distinguish "valid prefix" from "invalid" via response timing.
        authorized = any(hmac.compare_digest(token, k) for k in API_KEYS)
        if not authorized:
            client_ip = request.client.host if request.client else "unknown"
            log_event(
                "warning",
                "request.unauthorized",
                path="/v1/chat/completions",
                client_ip=client_ip,
            )
            raise HTTPException(status_code=401, detail="Invalid or missing API key")

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
    # Find last user message for preview
    for m in reversed(req.messages):
        if m.role == "user":
            s = _content_to_str(m.content)
            if s:
                record.user_message_preview = s[:100]
                break
    record.has_history = len(req.messages) > 1

    # Resolve the final think_level BEFORE logging request.start so the log shows
    # the actual value sent upstream (was previously logged as the pre-override
    # default, which was misleading for model=hy3-think + think_level=None).
    # hy3-think overrides think_level only when the user did NOT explicitly set it.
    # Using None as sentinel (set in Pydantic model), we can distinguish "explicit
    # no_think" from "not set".
    if req.think_level is None:
        think_level = "high" if req.model == "hy3-think" else DEFAULT_THINK_LEVEL
    else:
        think_level = req.think_level
    record.think_level = think_level

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

    msg, sys_prompt, history = messages_to_hy3(req.messages)
    # Validation must accept any message flow that includes a user turn somewhere
    # in the conversation, not just a non-empty last message. Tool result
    # round-trips legitimately end with role=tool and content may be null.
    has_user_msg = any(m.role == "user" and _content_to_str(m.content).strip() for m in req.messages)
    if not msg and not has_user_msg:
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
                "X-Max-Concurrent": str(MAX_CONCURRENT),
                "X-Active-Requests": str(limiter.active),
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
    finish_reason = "tool_calls" if final_tools else "stop"
    record.finish_reason = finish_reason
    record.status_code = 200
    record.finalize()

    # Compute duration directly instead of building a full to_dict() just for one field
    duration_ms = round((record.finished_at - record.started_at) * 1000, 1)
    log_event(
        "info",
        "request.done",
        request_id=request_id,
        status=200,
        duration_ms=duration_ms,
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

    # Estimate token usage (4 chars ≈ 1 token heuristic). Real counts require a
    # tokenizer; this is good enough for budget tracking.
    prompt_chars = sum(len(_content_to_str(m.content)) for m in req.messages)
    completion_chars = len(final_resp) + len(final_think)
    prompt_tokens = prompt_chars // 4
    completion_tokens = completion_chars // 4

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
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "total_tokens": prompt_tokens + completion_tokens,
        },
    }


async def stream_openai(
    completion_id: str,
    model: str,
    payload: dict,
    limiter: ConcurrencyLimiter,
    request_id: Optional[str] = None,
    record: Optional[RequestRecord] = None,
):
    """Yield OpenAI-format SSE chunks. Releases the limiter slot in finally.

    `limiter` is required — the caller must have already acquired a slot, and
    this function releases it in the finally block. Passing None would leak
    the slot, so the parameter is mandatory.
    """
    last_resp_len = 0
    last_think_len = 0
    final_tools: Optional[list] = None
    errored = False
    # capture created timestamp once and reuse across all chunks (OpenAI behavior)
    created_ts = int(time.time())

    log_event(
        "info",
        "stream.start",
        request_id=request_id,
        completion_id=completion_id,
        model=model,
    )

    # Initial role chunk
    yield f"data: {json.dumps(make_chunk(completion_id, model, role='assistant', created=created_ts))}\n\n"

    try:
        async for data in call_hy3_stream(payload, request_id=request_id, record=record):
            resp, think, tools, _ = parse_hy3_data(data)

            # Emit thinking deltas (as reasoning_content, OpenAI o1-style)
            if think and len(think) > last_think_len:
                chunk = make_chunk(
                    completion_id, model, reasoning=think[last_think_len:], created=created_ts
                )
                yield f"data: {json.dumps(chunk, ensure_ascii=False)}\n\n"
                last_think_len = len(think)
                if record:
                    record.response_thinking_chars = len(think)

            # Emit response deltas
            if resp and len(resp) > last_resp_len:
                chunk = make_chunk(
                    completion_id, model, content=resp[last_resp_len:], created=created_ts
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
            yield f"data: {json.dumps(make_chunk(completion_id, model, tool_calls=tc_delta, created=created_ts), ensure_ascii=False)}\n\n"
            yield f"data: {json.dumps(make_chunk(completion_id, model, finish_reason='tool_calls', created=created_ts))}\n\n"
            if record:
                record.response_tool_calls = len(final_tools)
                record.finish_reason = "tool_calls"
        else:
            yield f"data: {json.dumps(make_chunk(completion_id, model, finish_reason='stop', created=created_ts))}\n\n"
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
        # limiter is guaranteed non-None (required parameter).
        limiter.release(errored=errored)


# ----------------------- Admin Endpoints -----------------------

# All /admin/* endpoints require ADMIN_TOKEN. If unset, endpoints return 404
# (hide existence from attackers).
def _require_admin(request: Request):
    if not ADMIN_TOKEN:
        raise HTTPException(status_code=404, detail="Not Found")
    auth = request.headers.get("authorization", "")
    token = auth.removeprefix("Bearer ").strip() if auth.startswith("Bearer ") else ""
    if not token:
        token = request.headers.get("x-admin-token", "").strip()
    # use constant-time comparison to prevent timing attacks on admin token.
    if not hmac.compare_digest(token, ADMIN_TOKEN):
        raise HTTPException(status_code=401, detail="Unauthorized")


@app.get("/admin/logs")
async def admin_logs(
    request: Request,
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
    _require_admin(request)
    # call get_recent_logs ONCE — use get_log_summary() for the total count
    # instead of fetching all logs just to count them.
    filtered = get_recent_logs(
        limit=limit, level=level, request_id=request_id, event=event
    )
    total = get_log_summary()["total_logs"]
    return {
        "total": total,
        "returned": len(filtered),
        "filters": {"level": level, "request_id": request_id, "event": event, "limit": limit},
        "logs": filtered,
    }


@app.get("/admin/requests")
async def admin_requests(request: Request, limit: int = 50, errors_only: bool = False):
    """Return recent request records (newest first)."""
    _require_admin(request)
    # call get_recent_requests ONCE — REQUEST_BUFFER is capped at 200, so
    # use get_log_summary() for the total count instead of fetching all just to count.
    filtered = get_recent_requests(limit=limit, errors_only=errors_only)
    total = get_log_summary()["total_requests_tracked"]
    return {
        "total_tracked": total,
        "returned": len(filtered),
        "filters": {"limit": limit, "errors_only": errors_only},
        "requests": filtered,
    }


@app.get("/admin/logs/summary")
async def admin_logs_summary(request: Request):
    """Counts by level + event — for dashboard widgets."""
    _require_admin(request)
    return get_log_summary()


@app.get("/admin/requests/{request_id}")
async def admin_request_detail(request: Request, request_id: str):
    """Get a single request record + all log entries for it."""
    _require_admin(request)
    # REQUEST_BUFFER maxes at 200, so use the actual buffer cap.
    requests = [r for r in get_recent_requests(limit=200) if r["request_id"] == request_id]
    logs = get_recent_logs(limit=500, request_id=request_id)
    if not requests and not logs:
        raise HTTPException(status_code=404, detail=f"Request {request_id} not found")
    return {
        "request": requests[0] if requests else None,
        "logs": logs,
    }


# ----------------------- Entrypoint -----------------------


if __name__ == "__main__":
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
        default=_parse_int_env("PORT", 8000, min_val=1),
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
