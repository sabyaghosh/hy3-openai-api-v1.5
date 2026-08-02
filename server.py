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
from contextlib import asynccontextmanager
import hmac
import json
import os
import time
import uuid
from typing import Any, AsyncIterator, Literal, Optional, Union

import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, Field, field_validator
from starlette.exceptions import HTTPException as StarletteHTTPException

from logging_layer import (
    REQUEST_BUFFER_SIZE,
    RequestRecord,
    get_log_summary,
    get_recent_logs,
    get_recent_requests,
    log_event,
    new_request_id,
)

# Upstream Hy3 Gradio endpoint. Env-tunable so deployments can point at a
# different Space or self-hosted instance without a code change.
HY3_BASE = os.environ.get("HY3_BASE", "https://tencent-Hy3.hf.space/gradio_api/call/chat")
DEFAULT_MODEL = "hy3"
# Sane default output cap. The old 262144 (full context window) maximized
# latency and 504 risk against the read timeout. 4096 is enough for most
# chat responses; clients needing more can pass max_tokens explicitly.
DEFAULT_MAX_TOKENS = 4096
DEFAULT_THINK_LEVEL = "no_think"

# Single source of truth for the version string. Used by both the FastAPI app
# metadata (visible at /docs, /openapi.json) and the / endpoint.
# Keep in sync with: pyproject.toml [project].version and the CHANGELOG.md
# top entry. (1.4.2-1.4.4 were never released — no changelog entries existed.)
__version__ = "1.4.5"

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


def _parse_bool_env(name: str, default: bool) -> bool:
    raw = os.environ.get(name, str(default)).strip().lower()
    if raw in ("1", "true", "yes", "on"):
        return True
    if raw in ("0", "false", "no", "off", ""):
        return False
    print(f"Warning: {name}={raw!r} is not a valid bool, using {default}", flush=True)
    return default


# Whether to preserve thinking content in the Hy3 response. Passed as the 8th
# field in the Gradio data payload. When True, Hy3 includes reasoning text in
# the response so we can expose it as reasoning_content (OpenAI o1-style).
# Env-tunable since it inflates latency and token counts.
PRESERVED_THINKING = _parse_bool_env("PRESERVED_THINKING", True)
# Read timeout — configurable because gateway limits differ by platform.
# Default 90s is below Render's ~100s gateway timeout so the upstream call fails
# before the gateway returns 504 to the client. On platforms without a gateway
# timeout (Fly, Cloud Run), set HTTP_READ_TIMEOUT=300 for long thinking generations.
HTTP_READ_TIMEOUT = _parse_float_env("HTTP_READ_TIMEOUT", 90.0, min_val=1.0)
HTTP_TIMEOUT = httpx.Timeout(HTTP_READ_TIMEOUT, connect=30.0)

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
# Warn if API_KEYS was set but parsed to zero valid keys (e.g. API_KEYS=" , ")
if _API_KEYS_RAW and not API_KEYS:
    print(
        "Warning: API_KEYS was set but parsed to zero valid keys — "
        "running WITHOUT auth. Check for stray commas or whitespace.",
        flush=True,
    )
# Loud startup warning when running as an open proxy (no API keys at all).
if not API_KEYS:
    print(
        "WARNING: API_KEYS is not set — running as an OPEN PROXY. "
        "Anyone who can reach this service can use it without authentication. "
        "Set API_KEYS to require a Bearer token on /v1/chat/completions.",
        flush=True,
    )

# Timing-safe string comparison that tolerates non-ASCII characters.
# hmac.compare_digest(str, str) raises TypeError on non-ASCII; encoding to
# bytes avoids the crash. An unauthenticated caller sending a non-ASCII token
# would otherwise trigger a 500 instead of a 401.
def _constant_time_match(token: str, expected: str) -> bool:
    return hmac.compare_digest(token.encode("utf-8"), expected.encode("utf-8"))

# Optional admin token for /admin/* endpoints. If unset, admin endpoints return 404.
#   export ADMIN_TOKEN="my-secret-admin-token"
ADMIN_TOKEN = os.environ.get("ADMIN_TOKEN", "").strip()

# Input size limits (DoS protection)
MAX_MESSAGES = _parse_int_env("MAX_MESSAGES", 1000, min_val=1)
MAX_CONTENT_CHARS = _parse_int_env("MAX_CONTENT_CHARS", 1_000_000, min_val=1024)
# Reject request bodies larger than this BEFORE reading them (Starlette reads
# the full body before Pydantic validators run). Default 8MB is generous for
# any legitimate chat request; a 500MB payload is rejected at 413, not parsed.
MAX_BODY_BYTES = _parse_int_env("MAX_BODY_BYTES", 8_000_000, min_val=1024)

# Cap on a single SSE line from upstream. aiter_lines() is unbounded if the
# upstream sends no newline, so we check each line's length explicitly.
# Default 10MB — with DEFAULT_MAX_TOKENS=4096 a legitimate cumulative snapshot
# is well under 1MB, and even an explicit max_tokens=262144 stays under ~2MB.
SSE_BUFFER_CAP = _parse_int_env("SSE_BUFFER_CAP", 10_000_000, min_val=1024)

# Whether to trust X-Forwarded-For / X-Real-IP headers. Default true because
# every documented deployment platform puts a proxy in front of this service.
# Set TRUST_PROXY_HEADERS=false when exposing the port directly, otherwise any
# caller can spoof their logged client IP.
# NOTE: must be defined here, ABOVE _client_ip(), which reads it.
TRUST_PROXY_HEADERS = _parse_bool_env("TRUST_PROXY_HEADERS", True)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Manage shared resources across the application lifecycle.

    A shared httpx.AsyncClient eliminates per-request TCP+TLS handshakes to the
    upstream Hy3 host (~100-300ms saved per request at the cost of one open
    connection pool). Created once, closed on shutdown.

    Note: on shutdown, active SSE streams will error rather than complete
    (aclose() closes the connection pool). Acceptable at this scale.
    """
    app.state.http_client = httpx.AsyncClient(timeout=HTTP_TIMEOUT)
    try:
        yield
    finally:
        await app.state.http_client.aclose()


app = FastAPI(
    title="Hy3 OpenAI-Compatible API",
    version=__version__,
    description="OpenAI-compatible proxy for Tencent Hy3 295B MoE via HuggingFace Gradio API.",
    lifespan=lifespan,
)


# Reject oversized request bodies before they're parsed (413 Payload Too Large).
# This runs before Pydantic's MAX_MESSAGES/MAX_CONTENT_CHARS validators, which
# only fire after the full body has been buffered into memory.
@app.middleware("http")
async def _limit_body_size(request: Request, call_next):
    cl = request.headers.get("content-length")
    if cl is not None:
        try:
            if int(cl) > MAX_BODY_BYTES:
                return JSONResponse(
                    status_code=413,
                    content={"error": {
                        "message": f"Request body too large (max {MAX_BODY_BYTES} bytes)",
                        "type": "payload_too_large",
                    }},
                )
        except ValueError:
            pass
    return await call_next(request)


# ----------------------- OpenAI-shaped error responses -----------------------
# FastAPI's default error body is {"detail": ...}; Pydantic validation errors are
# {"detail": [ ... ]}. OpenAI clients parse {"error": {"message", "type", "code"}}.
# Previously only the 503 and 413 paths used the OpenAI shape, so 400/401/404/422/
# 502/504 surfaced in the SDK as opaque errors with no usable message. These two
# handlers normalise every error response to the OpenAI envelope.

_ERROR_TYPE_BY_STATUS = {
    400: "invalid_request_error",
    401: "authentication_error",
    403: "permission_error",
    404: "not_found_error",
    413: "payload_too_large",
    422: "invalid_request_error",
    429: "rate_limit_error",
    500: "internal_error",
    502: "upstream_error",
    503: "server_at_capacity",
    504: "upstream_timeout",
}


def _openai_error(
    status_code: int, message: str, code: Optional[str] = None
) -> JSONResponse:
    return JSONResponse(
        status_code=status_code,
        content={
            "error": {
                "message": message,
                "type": _ERROR_TYPE_BY_STATUS.get(status_code, "api_error"),
                "code": code,
            }
        },
    )


@app.exception_handler(StarletteHTTPException)
async def _http_exception_handler(request: Request, exc: StarletteHTTPException):
    # Registered on Starlette's HTTPException (FastAPI's subclasses it), so this
    # also covers router-raised 404/405. Preserve any headers the raiser attached
    # (e.g. Retry-After, WWW-Authenticate).
    resp = _openai_error(exc.status_code, str(exc.detail))
    if exc.headers:
        resp.headers.update(exc.headers)
    return resp


@app.exception_handler(RequestValidationError)
async def _validation_exception_handler(request: Request, exc: RequestValidationError):
    # Build the message from strings only. exc.errors() can embed a raw ValueError
    # under ctx["error"], which is not JSON-serialisable — returning it verbatim
    # would turn a 422 into a 500.
    parts: list[str] = []
    for err in exc.errors():
        loc = ".".join(str(x) for x in err.get("loc", ()) if x != "body")
        msg = str(err.get("msg", "invalid value"))
        parts.append(f"{loc}: {msg}" if loc else msg)
    return _openai_error(
        422, "; ".join(parts) or "Invalid request", code="validation_error"
    )


def _client_ip(request: Request) -> str:
    """Extract the client IP, preferring X-Forwarded-For when behind a proxy.

    On Render/Fly/Cloud Run/Koyeb, request.client.host is the load balancer's
    IP, not the end user's. X-Forwarded-For gives the real client. Only trust
    this header when a trusted proxy is in front (the default in production).
    Set TRUST_PROXY_HEADERS=false to disable and fall back to socket peer.
    """
    if TRUST_PROXY_HEADERS:
        xff = request.headers.get("x-forwarded-for", "")
        if xff:
            return xff.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


# CORS — allow the Next.js admin panel (and any OpenAI client) to call this API.
# Configure allowed origins via ADMIN_ORIGIN env var (default: permissive for dev).
# Supports a single origin or a comma-separated list of origins.
# "*" + credentials=true is invalid per CORS spec; browsers block credentialed
# requests when origin is "*". Use two distinct configurations.
_admin_origin_raw = os.environ.get("ADMIN_ORIGIN", "*")
if _admin_origin_raw == "*":
    # Wildcard mode — no credentials (spec-compliant)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_credentials=False,
        allow_methods=["*"],
        allow_headers=["*"],
    )
else:
    # Specific origin(s) — credentials allowed (spec-compliant).
    # ADMIN_ORIGIN can be a single origin or comma-separated list.
    _origins = [o.strip() for o in _admin_origin_raw.split(",") if o.strip()]
    app.add_middleware(
        CORSMiddleware,
        allow_origins=_origins,
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
        # Rolling window of the last 50 outcomes (True = errored). Used by
        # /health to detect transient upstream failures without a permanent
        # 503 brick from a single bad burst (cumulative counters never decay).
        from collections import deque
        self._recent_outcomes: deque = deque(maxlen=50)

    async def acquire(self) -> bool:
        """
        Try to acquire a slot within queue_timeout. Returns True on success,
        False if the queue expired (caller should return 503).

        For non-blocking mode (queue_timeout <= 0), uses a 1ms timeout as an
        approximation of "immediate" — asyncio.Semaphore has no try-acquire that
        can distinguish "available now" from "became available in 1ms", but the
        1ms ceiling is negligible in practice. For normal mode, uses the
        configured queue_timeout.

        If the client disconnects while queued, asyncio.wait_for raises
        CancelledError (not TimeoutError). We re-raise it so the caller's
        try/except can finalize the record — but we don't count it as a
        rejection (the client gave up, not the server).
        """
        timeout = self.queue_timeout if self.queue_timeout > 0 else 0.001
        try:
            await asyncio.wait_for(self._sem.acquire(), timeout=timeout)
        except asyncio.TimeoutError:
            self.total_rejected += 1
            return False
        except asyncio.CancelledError:
            # Client disconnected while waiting in queue. Re-raise so the caller
            # can clean up. Don't increment total_rejected (client gave up, not
            # server overload). Don't touch the semaphore (we never acquired it).
            raise
        self.active += 1
        self.total_acquired += 1
        if self.active > self.peak_active:
            self.peak_active = self.active
        return True

    def release(self, *, errored: bool = False) -> None:
        # Guard against active counter underflow on double-release. If active is
        # already 0, this release is spurious — log it (it indicates a bug in
        # the caller's release logic) and return without touching the semaphore.
        # NOTE: a genuine double-release silently leaks a semaphore slot because
        # we skip _sem.release() here. If you see release_underflow warnings,
        # investigate the caller — the _slot context manager below prevents this
        # structurally.
        if self.active <= 0:
            log_event("warning", "limiter.release_underflow", active=self.active)
            return
        self.active -= 1
        self._recent_outcomes.append(errored)
        # An errored request should NOT count as 'completed'; track errors
        # separately so total_acquired == total_completed + total_errors.
        if errored:
            self.total_errors += 1
        else:
            self.total_completed += 1
        self._sem.release()

    def recent_error_rate(self) -> tuple[int, float]:
        """Return (window_size, error_rate) over the last 50 outcomes."""
        n = len(self._recent_outcomes)
        if n == 0:
            return 0, 0.0
        return n, sum(self._recent_outcomes) / n

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


@asynccontextmanager
async def _slot(limiter: "ConcurrencyLimiter"):
    """Own exactly one limiter slot; release exactly once, no matter how we exit.

    This prevents double-release (which leaks semaphore capacity) and
    forgotten-release (which leaks the slot forever). Use:

        acquired = await limiter.acquire()
        if not acquired:
            return JSONResponse(..., status_code=503)
        async with _slot(limiter):
            ...  # do upstream work

    Cancellations (client disconnect) are NOT counted as errors — they're
    client-side give-ups, not server failures. Counting them would inflate
    the error rate and trip /health's degradation check under normal traffic.
    """
    released = False
    try:
        yield
    except (asyncio.CancelledError, GeneratorExit):
        # Client gave up; not a server error. Set released=True BEFORE calling
        # release() so a failure inside release() can't trigger a second release.
        released = True
        limiter.release(errored=False)
        raise
    except BaseException:
        released = True
        limiter.release(errored=True)
        raise
    finally:
        if not released:
            limiter.release(errored=False)


limiter = ConcurrencyLimiter(MAX_CONCURRENT, QUEUE_TIMEOUT)


# ----------------------- Pydantic Models -----------------------


class ChatMessage(BaseModel):
    # Literal catches typos like "usr" or "asistant" at 422 instead of
    # forwarding garbage to Hy3. "developer" is OpenAI's newer name for system.
    role: Literal["system", "user", "assistant", "tool", "developer"]
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
    # ge=1 rejects 0, negative, and (with the explicit is None check below)
    # ensures max_tokens is always a positive int when forwarded upstream.
    max_tokens: Optional[int] = Field(DEFAULT_MAX_TOKENS, ge=1)
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
    # n: Hy3 only supports n=1. We accept n=1 or n=None (not specified) for
    # compatibility. n>1 returns 422 via the validator below.
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
        # Hy3 only supports n=1 (single completion). Reject n>1 explicitly.
        # n=None (explicit null) is accepted as "not specified" — same as omitting.
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
        # it becomes the conversation history.
        last = non_system[-1]
        if last.role == "user":
            # Common case: last message is the user's prompt.
            last_user_msg = _content_to_str(last.content)
            prior = non_system[:-1]
        else:
            # Last turn is assistant/tool (e.g. tool-result round-trip). Keep
            # history in chronological order and synthesize the current prompt
            # from the trailing turn, so tool results are presented as "what just
            # happened" rather than reordering the conversation (which would
            # pull a past user question out of sequence and confuse the model).
            prior = non_system[:-1]
            tail = _content_to_str(last.content).strip()
            if last.role == "tool":
                last_user_msg = (
                    f"Tool result for call {last.tool_call_id or 'unknown'}:\n{tail}"
                    if tail else
                    f"Tool call {last.tool_call_id or 'unknown'} returned no output."
                )
            else:
                # assistant continuation — ask the model to continue.
                last_user_msg = tail or "Continue."

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

    # Use the shared httpx.AsyncClient created in the FastAPI lifespan. This
    # eliminates per-request TCP+TLS handshakes to the upstream Hy3 host
    # (~100-300ms saved per request).
    client = app.state.http_client

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

    # Step 2: stream SSE.
    # Hy3 sends cumulative snapshots (each chunk contains the FULL response so far,
    # not an incremental delta). Both the streaming and non-streaming paths rely on
    # this invariant — if upstream ever switches to deltas, non-streaming returns
    # only the final fragment while streaming produces garbled output.
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
            first_chunk_latency_ms: Optional[float] = None
            # Use aiter_lines() for SSE framing — it handles newline splitting
            # internally and avoids the O(n²) buffer += chunk pattern.
            # aiter_lines() is unbounded if the upstream sends no newline, so we
            # cap each line's length at SSE_BUFFER_CAP to prevent memory exhaustion
            # from a malformed/hostile upstream.
            async for line in resp.aiter_lines():
                if len(line) > SSE_BUFFER_CAP:
                    log_event(
                        "error",
                        "upstream.stream.line_too_large",
                        request_id=request_id,
                        size=len(line),
                        cap=SSE_BUFFER_CAP,
                    )
                    raise HTTPException(
                        status_code=502,
                        detail=f"Hy3 stream line exceeded size cap ({len(line)} > {SSE_BUFFER_CAP})",
                    )
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


def _safe_idx(lst: list, i: int, default: Any) -> Any:
    """Safely index a list, returning default if out of range or falsy."""
    if i < len(lst) and lst[i]:
        return lst[i]
    return default


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
    resp = _safe_idx(inner, 0, "")
    think = _safe_idx(inner, 1, "")
    tools = _safe_idx(inner, 2, [])
    hist = _safe_idx(inner, 3, [])
    return resp or "", think or "", tools or [], hist or []


def make_chunk(
    completion_id: str,
    model: str,
    *,
    content: Optional[str] = None,
    reasoning: str = "",
    role: Optional[str] = None,
    tool_calls: Optional[list] = None,
    finish_reason: Optional[str] = None,
    created: Optional[int] = None,
) -> dict:
    delta: dict = {}
    if role:
        delta["role"] = role
    # Include content in the delta when explicitly provided (even if empty string).
    # OpenAI emits {"role":"assistant","content":""} on the first chunk; strict
    # parsers expect the key. None (default) means "don't include the key".
    if content is not None:
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

    Returns 503 when the upstream error rate over the last 50 requests exceeds
    50% (with at least 10 in the window). Uses a sliding window so a transient
    upstream blip doesn't brick the service permanently — once the bad requests
    age out of the window, /health recovers automatically.
    """
    s = limiter.stats()
    window_n, window_rate = limiter.recent_error_rate()
    error_rate_high = window_n >= 10 and window_rate > 0.5
    healthy = not error_rate_high
    status = "ok" if healthy else "degraded"
    return JSONResponse(
        status_code=200 if healthy else 503,
        content={
            "status": status,
            "recent_window": window_n,
            "recent_error_rate": round(window_rate, 3),
            **s,
        },
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
        # RFC 7235: the auth scheme is case-insensitive. Accept "Bearer", "bearer", etc.
        token = ""
        if auth:
            parts = auth.split(None, 1)  # split on first whitespace
            if len(parts) == 2 and parts[0].lower() == "bearer":
                token = parts[1].strip()
        # Also allow raw API key in x-api-key header (some clients use this)
        if not token:
            token = request.headers.get("x-api-key", "").strip()
        # Use constant-time comparison to prevent timing attacks. We iterate
        # over ALL keys (no short-circuit via any()) so the comparison count
        # doesn't leak which key matched. _constant_time_match encodes to bytes
        # to avoid TypeError on non-ASCII tokens.
        authorized = False
        for k in API_KEYS:
            if _constant_time_match(token, k):
                authorized = True
        if not authorized:
            log_event(
                "warning",
                "request.unauthorized",
                path="/v1/chat/completions",
                client_ip=_client_ip(request),
            )
            raise HTTPException(status_code=401, detail="Invalid or missing API key")

    # Per-request ID + record for tracing through the entire pipeline
    request_id = new_request_id()
    client_ip = _client_ip(request)
    # Capture created timestamp at request time (OpenAI stamps at request time,
    # not after generation — a 60s thinking run shouldn't skew the timestamp).
    created_ts = int(time.time())
    completion_id = f"chatcmpl-{uuid.uuid4().hex[:24]}"
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
    # Note: has_history is set after messages_to_hy3() below, from the actual
    # history list (a single system + single user message has history=[]).

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
    record.has_history = bool(history)  # set from actual history, not message count
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
        # Use explicit `is None` check — `or` would treat 0 as falsy (but Field(ge=1)
        # already rejects 0, so this is belt-and-suspenders).
        max_tokens=req.max_tokens if req.max_tokens is not None else DEFAULT_MAX_TOKENS,
        temperature=req.temperature,
        top_p=req.top_p,
        tools=req.tools,
    )

    # ----- Concurrency cap: acquire a slot before touching upstream Hy3 -----
    t_queue_start = time.perf_counter()
    record.concurrency_active_at_start = limiter.active
    # Wrap acquire() in try/except CancelledError so a client disconnect while
    # queued finalizes the record (otherwise it's invisible in /admin/requests).
    try:
        acquired = await limiter.acquire()
    except asyncio.CancelledError:
        record.status_code = 499  # nginx convention: client closed request
        record.error = "client disconnected while queued"
        record.queued_ms = round((time.perf_counter() - t_queue_start) * 1000, 1)
        record.finalize()
        raise
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
                created_ts=created_ts,
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

    # Non-streaming: collect full response. The _slot context manager handles
    # limiter release in both success and error paths (no manual try/finally).
    final_resp = ""
    final_think = ""
    final_tools: list = []
    try:
        async with _slot(limiter):
            async for data in call_hy3_stream(payload, request_id=request_id, record=record):
                # Hy3 sends cumulative snapshots: each chunk contains the FULL
                # response so far. We replace (not append) to keep only the latest.
                # If upstream ever switches to incremental deltas, this would
                # return only the final fragment — the streaming path has the
                # same assumption (see call_hy3_stream docstring).
                resp, think, tools, _ = parse_hy3_data(data)
                if resp:
                    final_resp = resp
                if think:
                    final_think = think
                if tools:
                    final_tools = tools
    except HTTPException as e:
        record.status_code = e.status_code
        record.error = str(e.detail)[:300]
        record.finalize()  # C3: finalize so errors appear in /admin/requests
        log_event(
            "error",
            "request.upstream_http_error",
            request_id=request_id,
            status=e.status_code,
            error=str(e.detail)[:300],
        )
        raise
    except Exception as e:
        record.status_code = 502
        record.error = str(e)[:300]
        record.finalize()  # C3: finalize so errors appear in /admin/requests
        log_event(
            "error",
            "request.unhandled_error",
            request_id=request_id,
            error=f"{type(e).__name__}: {e}",
        )
        raise HTTPException(status_code=502, detail=f"Hy3 call failed: {e}")

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

    # OpenAI spec: content should be null (not "") when tool_calls is present.
    message: dict = {"role": "assistant", "content": final_resp or None}
    if final_think:
        message["reasoning_content"] = final_think
    if final_tools:
        message["tool_calls"] = make_tool_call_objects(final_tools)

    # Estimate token usage (4 chars ≈ 1 token heuristic). Real counts require a
    # tokenizer; this is good enough for budget tracking. Include the serialized
    # tools payload in the prompt count — tool schemas can be thousands of tokens
    # in agentic workloads, and omitting them systematically underestimates cost.
    prompt_chars = sum(len(_content_to_str(m.content)) for m in req.messages)
    if req.tools:
        prompt_chars += len(json.dumps(req.tools, ensure_ascii=False))
    completion_chars = len(final_resp) + len(final_think)
    prompt_tokens = prompt_chars // 4
    completion_tokens = completion_chars // 4

    # M5: return JSONResponse so we can attach X-Request-Id (was missing on 200).
    return JSONResponse(
        content={
            "id": completion_id,
            "object": "chat.completion",
            "created": created_ts,
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
        },
        headers={"X-Request-Id": request_id},
    )


async def stream_openai(
    completion_id: str,
    model: str,
    payload: dict,
    limiter: ConcurrencyLimiter,
    request_id: Optional[str] = None,
    record: Optional[RequestRecord] = None,
    created_ts: Optional[int] = None,
):
    """Yield OpenAI-format SSE chunks. Releases the limiter slot in finally.

    `limiter` is required — the caller must have already acquired a slot, and
    this function releases it in the finally block. Passing None would leak
    the slot, so the parameter is mandatory.

    `created_ts` is the request timestamp (captured at request start, not after
    generation) — matches OpenAI's behavior.

    Note: we use a manual try/finally rather than the _slot context manager
    because this is an async generator — the yield points make context manager
    semantics tricky (the body suspends at each yield). The finally block
    runs on both normal completion and client disconnect (generator.close()).
    """
    last_resp_len = 0
    last_think_len = 0
    final_tools: Optional[list] = None
    errored = False
    if created_ts is None:
        created_ts = int(time.time())
    # Track the previous cumulative snapshot to detect divergence (H6).
    prev_resp = ""

    log_event(
        "info",
        "stream.start",
        request_id=request_id,
        completion_id=completion_id,
        model=model,
    )

    # Initial role chunk. Include content="" so strict parsers see the key
    # (OpenAI emits {"role":"assistant","content":""}).
    yield f"data: {json.dumps(make_chunk(completion_id, model, role='assistant', content='', created=created_ts))}\n\n"

    try:
        async for data in call_hy3_stream(payload, request_id=request_id, record=record):
            resp, think, tools, _ = parse_hy3_data(data)

            # H6: detect non-monotonic snapshots. Hy3 should send cumulative
            # snapshots (each chunk is a superset of the previous). If it ever
            # sends a diverged or shorter snapshot (retry, regeneration), we
            # re-sync by re-emitting from the start.
            if resp and prev_resp and not resp.startswith(prev_resp):
                log_event(
                    "warning",
                    "stream.snapshot_diverged",
                    request_id=request_id,
                    prev_len=len(prev_resp),
                    new_len=len(resp),
                )
                last_resp_len = 0  # re-emit from start
            if resp:
                prev_resp = resp

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
        # Client disconnect raises GeneratorExit at the yield point. GeneratorExit
        # derives from BaseException, so NEITHER `except HTTPException` nor
        # `except Exception` above catches it — without this block the record is
        # never finalized and the aborted stream is invisible in /admin/requests.
        # finalize() is idempotent, but we only stamp 499 when nothing else has.
        if record is not None and not record.finalized:
            record.status_code = 499  # nginx convention: client closed request
            record.error = record.error or "client disconnected mid-stream"
            record.finish_reason = record.finish_reason or "cancelled"
            record.finalize()
            log_event(
                "warning",
                "stream.client_disconnected",
                request_id=request_id,
                response_chars=last_resp_len,
                thinking_chars=last_think_len,
            )
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
    # Case-insensitive Bearer scheme (RFC 7235).
    token = ""
    if auth:
        parts = auth.split(None, 1)
        if len(parts) == 2 and parts[0].lower() == "bearer":
            token = parts[1].strip()
    if not token:
        token = request.headers.get("x-admin-token", "").strip()
    # Use constant-time comparison to prevent timing attacks on admin token.
    # _constant_time_match encodes to bytes to avoid TypeError on non-ASCII.
    if not _constant_time_match(token, ADMIN_TOKEN):
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
    # Scan the whole request ring buffer. Import the cap from logging_layer
    # rather than hardcoding 200 — a literal here silently truncates the scan
    # if REQUEST_BUFFER_SIZE is ever raised.
    requests = [
        r
        for r in get_recent_requests(limit=REQUEST_BUFFER_SIZE)
        if r["request_id"] == request_id
    ]
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
