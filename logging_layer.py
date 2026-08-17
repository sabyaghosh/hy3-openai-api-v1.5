"""
Structured logging + in-memory ring buffer for the Hy3 OpenAI-compatible API.

Provides:
  - log_event(level, event, **fields) — structured log to stdout + ring buffer
  - RequestRecord — per-request lifecycle record (start, upstream calls, finish)
  - get_recent_logs(limit, level, request_id) — query the ring buffer
  - get_recent_requests(limit) — query summarized request records

The ring buffer is bounded (LOG_BUFFER=2000 entries, REQUEST_BUFFER=200 entries)
and process-local. For production multi-instance deployments, replace with
Redis/Postgres.
"""

import json
import logging
import os
import sys
import time
import uuid
from collections import deque
from typing import Any, Optional

# ----------------------- Logger setup -----------------------

LOG_LEVEL = os.environ.get("LOG_LEVEL", "INFO").upper()

logger = logging.getLogger("hy3")
logger.setLevel(getattr(logging, LOG_LEVEL, logging.INFO))
if not logger.handlers:
    h = logging.StreamHandler(sys.stdout)
    # Use UTC (converter=time.gmtime) for the stdout formatter so it matches
    # ts_iso and started_at_iso in the ring buffer. Correlating stdout against
    # /admin/logs on a non-UTC host would otherwise silently mislead.
    h.setFormatter(
        logging.Formatter(
            fmt="%(asctime)s.%(msecs)03dZ [%(levelname)s] %(message)s",
            datefmt="%Y-%m-%dT%H:%M:%S",
        )
    )
    h.formatter.converter = time.gmtime  # UTC instead of local time
    logger.addHandler(h)


# ----------------------- Ring buffer -----------------------

BUFFER_SIZE = 2000  # keep last 2000 log entries
REQUEST_BUFFER_SIZE = 200  # keep last 200 request summaries
LOG_BUFFER: deque = deque(maxlen=BUFFER_SIZE)
REQUEST_BUFFER: deque = deque(maxlen=REQUEST_BUFFER_SIZE)


def _truncate(s: Any, n: int = 500) -> Any:
    """Truncate a value for the ring buffer. Preserves scalar types (int,
    float) so dashboards can do numeric comparison without re-parsing.
    Strings and complex types are truncated to n chars with a suffix indicator.

    v1.5.5 (C10): removed `bool` from the isinstance tuple — it's redundant
    because `bool` subclasses `int` (`isinstance(True, int)` is already True).
    Behavior is unchanged; this just removes the dead branch.
    """
    if s is None:
        return ""
    if isinstance(s, (int, float)):
        return s  # preserve scalar type — don't stringify
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
        "ts_iso": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(ts))
        + f".{int((ts % 1) * 1000):03d}Z",
        "level": level.lower(),
        "event": event,
        "request_id": request_id,
        "fields": {k: _truncate(v, 500) for k, v in fields.items()},
    }
    LOG_BUFFER.append(entry)

    # Also log to stdout (compact one-liner).
    # Note: `if v != ""` filters empty-string field values from the one-liner
    # to keep it concise. Other falsy values like "0" or "[]" are preserved
    # because they carry information (e.g., tools_count=0, error=[]).
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
        self._finalized = False  # guard against double-finalize
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
        self.upstream_post_latency_ms: Optional[float] = None
        self.upstream_stream_latency_ms: Optional[float] = None
        self.upstream_chunks: int = 0
        # Response details
        self.response_chars: int = 0
        self.response_thinking_chars: int = 0
        self.response_tool_calls: int = 0
        self.finish_reason: Optional[str] = None
        # Concurrency at the time of request
        self.concurrency_active_at_start: Optional[int] = None
        self.queued_ms: Optional[float] = None

    def to_dict(self) -> dict:
        return {
            "request_id": self.request_id,
            "method": self.method,
            "path": self.path,
            "client_ip": self.client_ip,
            "started_at": self.started_at,
            # v1.5.5 (C8): add millisecond precision to match log_event's
            # ts_iso format. Makes cross-referencing /admin/requests and
            # /admin/logs timestamps easier (was whole-second only).
            "started_at_iso": time.strftime(
                "%Y-%m-%dT%H:%M:%S", time.gmtime(self.started_at)
            ) + f".{int((self.started_at % 1) * 1000):03d}Z",
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
            # v1.5.5 (C7): no _truncate here — server.py already truncates
            # user_message_preview to 100 chars at assignment time. Double
            # truncation was redundant (and _truncate on a short string is
            # a no-op anyway, but this makes the data flow explicit).
            "user_message_preview": self.user_message_preview,
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

    @property
    def finalized(self) -> bool:
        """Public read-only view of the finalize guard.

        Callers in cleanup paths (e.g. an async generator's `finally` block on
        client disconnect) need to know whether the record already made it into
        REQUEST_BUFFER, so they can stamp a terminal status before finalizing.
        Exposed as a property so they don't have to touch `_finalized`.
        """
        return self._finalized

    def finalize(self) -> None:
        # Idempotent — safe to call multiple times.
        if self._finalized:
            return
        self._finalized = True
        self.finished_at = time.time()
        REQUEST_BUFFER.append(self.to_dict())


# ----------------------- Query helpers -----------------------


def get_recent_logs(
    limit: int = 100,
    level: Optional[str] = None,
    request_id: Optional[str] = None,
    event: Optional[str] = None,
) -> list[dict]:
    """Return recent log entries, newest first, filtered by criteria.

    `limit <= 0` returns an empty list. Without this guard the append-then-test
    loop below emits one entry for limit=0, and a negative limit behaves the
    same as limit=1 — both reachable from the /admin/logs query string.
    """
    if limit <= 0:
        return []
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
    """Return recent request records, newest first.

    `limit <= 0` returns an empty list. Guard is required because `entries[:limit]`
    with a negative limit silently returns "all but the last N" records rather
    than nothing — a confusing result for `/admin/requests?limit=-5`.
    """
    if limit <= 0:
        return []
    entries = list(REQUEST_BUFFER)
    entries.reverse()
    if errors_only:
        # status_code is always set before finalize(), so just check >= 400.
        entries = [r for r in entries if r["status_code"] is not None and r["status_code"] >= 400]
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
