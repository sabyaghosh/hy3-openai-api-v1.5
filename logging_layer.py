"""
Structured logging + in-memory ring buffer for the Hy3 OpenAI-compatible API.

Provides:
  - log_event(level, event, **fields) — structured log to stdout + ring buffer
  - RequestTracker — per-request context manager that captures full lifecycle
  - get_recent_logs(limit, level, request_id) — query the ring buffer
  - get_recent_requests(limit) — query summarized request records

The ring buffer is bounded (default 1000 entries) and process-local.
For production multi-instance deployments, replace with Redis/Postgres.
"""

import asyncio
import json
import logging
import sys
import time
import traceback
import uuid
from collections import deque
from contextlib import asynccontextmanager
from typing import Any, Optional

# ----------------------- Logger setup -----------------------

logger = logging.getLogger("hy3")
logger.setLevel(logging.DEBUG)
if not logger.handlers:
    h = logging.StreamHandler(sys.stdout)
    h.setFormatter(
        logging.Formatter(
            fmt="%(asctime)s.%(msecs)03d [%(levelname)s] %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        )
    )
    logger.addHandler(h)


# ----------------------- Ring buffer -----------------------

BUFFER_SIZE = 2000  # keep last 2000 log entries
LOG_BUFFER: deque = deque(maxlen=BUFFER_SIZE)
REQUEST_BUFFER: deque = deque(maxlen=200)  # last 200 request summaries

# Per-request log index: request_id -> list of log entries (built lazily)


def _truncate(s: Any, n: int = 500) -> str:
    if s is None:
        return ""
    if not isinstance(s, str):
        try:
            s = json.dumps(s, ensure_ascii=False, default=str)
        except Exception:
            s = str(s)
    return s if len(s) <= n else s[:n] + f"...(+{len(s) - n} more)"


def log_event(
    level: str,
    event: str,
    *,
    request_id: Optional[str] = None,
    **fields: Any,
) -> None:
    """
    Log a structured event. Goes to stdout (via logger) AND the ring buffer.

    level: debug | info | warning | error
    event: short event name, e.g. "request.start", "upstream.post", "upstream.stream_chunk"
    fields: arbitrary structured fields (auto-truncated to keep entries small)
    """
    ts = time.time()
    entry = {
        "ts": ts,
        "ts_iso": time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime(ts))
        + f".{int((ts % 1) * 1000):03d}",
        "level": level.lower(),
        "event": event,
        "request_id": request_id,
        "fields": {k: _truncate(v, 500) for k, v in fields.items()},
    }
    LOG_BUFFER.append(entry)

    # Also log to stdout (compact one-liner)
    fld_str = " ".join(f"{k}={v}" for k, v in entry["fields"].items() if v != "")
    rid = f"[{request_id[:8]}] " if request_id else ""
    msg = f"{rid}{event} {fld_str}".strip()
    if level == "error":
        logger.error(msg)
    elif level == "warning":
        logger.warning(msg)
    elif level == "info":
        logger.info(msg)
    else:
        logger.debug(msg)


# ----------------------- Request tracker -----------------------


class RequestRecord:
    """Summarized record of a single API request lifecycle."""

    def __init__(self, request_id: str, method: str, path: str, client_ip: str):
        self.request_id = request_id
        self.method = method
        self.path = path
        self.client_ip = client_ip
        self.started_at = time.time()
        self.finished_at: Optional[float] = None
        self.status_code: Optional[int] = None
        self.error: Optional[str] = None
        # Request details
        self.model: Optional[str] = None
        self.stream: bool = False
        self.has_tools: bool = False
        self.has_history: bool = False
        self.message_count: int = 0
        self.user_message_preview: str = ""
        self.think_level: Optional[str] = None
        # Upstream Hy3 details
        self.upstream_event_id: Optional[str] = None
        self.upstream_post_status: Optional[int] = None
        self.upstream_post_latency_ms: Optional[int] = None
        self.upstream_stream_latency_ms: Optional[int] = None
        self.upstream_chunks: int = 0
        # Response details
        self.response_chars: int = 0
        self.response_thinking_chars: int = 0
        self.response_tool_calls: int = 0
        self.finish_reason: Optional[str] = None
        # Concurrency at the time of request
        self.concurrency_active_at_start: Optional[int] = None
        self.queued_ms: Optional[int] = None

    def to_dict(self) -> dict:
        return {
            "request_id": self.request_id,
            "method": self.method,
            "path": self.path,
            "client_ip": self.client_ip,
            "started_at": self.started_at,
            "started_at_iso": time.strftime(
                "%Y-%m-%d %H:%M:%S", time.gmtime(self.started_at)
            ),
            "finished_at": self.finished_at,
            "duration_ms": (
                round((self.finished_at - self.started_at) * 1000, 1)
                if self.finished_at
                else None
            ),
            "status_code": self.status_code,
            "error": self.error,
            "model": self.model,
            "stream": self.stream,
            "has_tools": self.has_tools,
            "has_history": self.has_history,
            "message_count": self.message_count,
            "user_message_preview": _truncate(self.user_message_preview, 100),
            "think_level": self.think_level,
            "upstream_event_id": self.upstream_event_id,
            "upstream_post_status": self.upstream_post_status,
            "upstream_post_latency_ms": self.upstream_post_latency_ms,
            "upstream_stream_latency_ms": self.upstream_stream_latency_ms,
            "upstream_chunks": self.upstream_chunks,
            "response_chars": self.response_chars,
            "response_thinking_chars": self.response_thinking_chars,
            "response_tool_calls": self.response_tool_calls,
            "finish_reason": self.finish_reason,
            "concurrency_active_at_start": self.concurrency_active_at_start,
            "queued_ms": self.queued_ms,
        }

    def finalize(self) -> None:
        self.finished_at = time.time()
        REQUEST_BUFFER.append(self.to_dict())


# ----------------------- Query helpers -----------------------


def get_recent_logs(
    limit: int = 100,
    level: Optional[str] = None,
    request_id: Optional[str] = None,
    event: Optional[str] = None,
) -> list[dict]:
    """Return recent log entries, newest first, filtered by criteria."""
    out = []
    entries = list(LOG_BUFFER)
    entries.reverse()  # newest first
    for e in entries:
        if level and e["level"] != level.lower():
            continue
        if request_id and e["request_id"] != request_id:
            continue
        if event and e["event"] != event:
            continue
        out.append(e)
        if len(out) >= limit:
            break
    return out


def get_recent_requests(limit: int = 50, errors_only: bool = False) -> list[dict]:
    """Return recent request records, newest first."""
    entries = list(REQUEST_BUFFER)
    entries.reverse()
    if errors_only:
        entries = [r for r in entries if r["status_code"] is None or r["status_code"] >= 400]
    return entries[:limit]


def get_log_summary() -> dict:
    """Counts by level + event for the admin dashboard."""
    counts_by_level: dict[str, int] = {}
    counts_by_event: dict[str, int] = {}
    for e in LOG_BUFFER:
        counts_by_level[e["level"]] = counts_by_level.get(e["level"], 0) + 1
        counts_by_event[e["event"]] = counts_by_event.get(e["event"], 0) + 1
    return {
        "total_logs": len(LOG_BUFFER),
        "total_requests_tracked": len(REQUEST_BUFFER),
        "buffer_capacity": BUFFER_SIZE,
        "by_level": counts_by_level,
        "by_event": counts_by_event,
    }


def new_request_id() -> str:
    return uuid.uuid4().hex
