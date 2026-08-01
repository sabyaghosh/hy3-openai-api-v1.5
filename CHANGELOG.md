# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [1.3.0] — 2025-08-01

Comprehensive bug-fix release. 25 surgical fixes over the original `hy3-openai-api`.

### 🔴 Critical Fixes

- **`Dockerfile` now copies `logging_layer.py`** — Docker images were crashing on startup with `ModuleNotFoundError: No module named 'logging_layer'`. Every Docker/Render/Koyeb/Fly deployment was broken.
- **CORS spec compliance** — `Access-Control-Allow-Origin: *` is no longer paired with `Access-Control-Allow-Credentials: true`. Browsers were blocking all credentialed requests. Now uses two distinct configurations: wildcard mode (no credentials) or specific origin (credentials allowed). Configure via `ADMIN_ORIGIN` env var.
- **README install URL fixed** — was pointing to `YOUR_USER/hy3-client` (placeholder + wrong repo name). Now correctly points to `sabyaghosh/hy3-openai-api`.

### 🟠 Security

- **Admin endpoints require `ADMIN_TOKEN`** — `/admin/logs`, `/admin/requests`, `/admin/logs/summary`, `/admin/requests/{id}` now return 404 if `ADMIN_TOKEN` is unset (hides existence from attackers) and 401 if the token is wrong. Previously, anyone on the internet could read client IPs, user message previews, and error details.
- **Optional API key validation** — set `API_KEYS=key1,key2,key3` to require `Authorization: Bearer <key>` (or `x-api-key` header) on `/v1/chat/completions`. If unset, the API is open (backward-compatible).
- **Input size limits** — `MAX_MESSAGES` (default 1000) and `MAX_CONTENT_CHARS` (default 1,000,000) prevent DoS via huge payloads. Oversized requests return 422.
- **`hy3.sh` path traversal blocked** — conversation IDs passed via `-c` are now sanitized to `^[a-zA-Z0-9_-]+$`. Previously `../../etc/passwd` would write outside `~/.hy3_state/`.

### 🟡 Correctness Fixes

- **`ConcurrencyLimiter.release()`** — no longer underflows the `active` counter on double-release. Spurious releases (when `active` is already 0) are detected via `limiter.release_underflow` warning log and do not increment any counter. The invariant `total_acquired == total_completed + total_errors` now always holds.
- **`total_completed` no longer double-counted on errors** — previously an errored request incremented both `total_completed` and `total_errors`. Now `total_completed` counts only successful releases; `total_errors` counts errored releases.
- **Dead `httpx.HTTPStatusError` handler reactivated** — the `except httpx.HTTPStatusError` block in `stream_openai` was dead code (never triggered because `call_hy3_stream` raised `HTTPException` instead). Now properly wired up to handle upstream HTTP errors.
- **Tool-call round-trips no longer rejected** — messages ending with `role=tool` and `content=null` (valid OpenAI spec for empty tool results) were rejected with 400 "No user message provided". Now accepts any message flow that includes at least one user turn.
- **`think_level` override respects explicit user choice** — passing `think_level="no_think"` with `model="hy3-think"` used to be overridden to `"high"` (because the default was also `"no_think"` and the code couldn't distinguish "explicit no_think" from "not set"). Now uses `None` as sentinel; the `hy3-think` override only triggers when the user did not explicitly set `think_level`.
- **`think_level` validated** — passing an invalid value (e.g. `"medium"`) now returns 422 instead of being silently forwarded to upstream.
- **`RequestRecord.finalize()` is idempotent** — calling it twice (which could happen in edge cases) no longer double-appends to the request buffer. Added `_finalized` guard.
- **Token usage is now estimated** — `usage.prompt_tokens`, `completion_tokens`, `total_tokens` are computed with a 4-chars-≈-1-token heuristic. Previously hardcoded to 0, which broke cost tracking in many OpenAI clients.

### 🟢 Code Quality

- **Env var parsing with validation** — `MAX_CONCURRENT=abc` no longer crashes startup with a confusing `ValueError`. Bad values fall back to defaults with a warning. `MAX_CONCURRENT=0` or negative values are clamped to minimum 1.
- **Pinned dependency upper bounds** in `requirements.txt` — `fastapi>=0.110,<0.120`, `pydantic>=2.5,<3.0`, etc. Prevents future major versions from breaking the API.
- **Pinned Python base image** in `Dockerfile` — `python:3.11.9-slim-bookworm` instead of unpinned `python:3.11-slim` for reproducible builds.
- **`hy3.sh` curl errors no longer swallowed** — was `curl -sf -N ... 2>/dev/null` which hid all errors and produced empty output. Now captures exit code, checks for empty output, and prints a helpful error message.
- **`hy3.sh` file handle leak fixed** — `open(tmpfile).read()` (never closed) replaced with `with open(tmpfile, encoding='utf-8') as f:`.
- **`hy3.sh` bare `except:` replaced** with `except Exception:` — was catching `KeyboardInterrupt` and `SystemExit`, making the script hard to interrupt.
- **`hy3.sh` `TOOL_CALL` format changed** — old format `TOOL_CALL:id:name:args` was ambiguous when args contained colons (e.g. `{"time": "12:30"}`). New format `TOOL_CALL:{json_envelope}` is unambiguous.
- **`hy3.sh` python3 check** — script now verifies `python3` is installed before depending on it, with a clear error message.
- **`hy3.sh` conversation save failure no longer silent** — was `except Exception: pass`. Now prints a warning to stderr.
- **Configurable log level** — `LOG_LEVEL` env var (default `INFO`). Previously hardcoded to `DEBUG` which flooded stdout in production.
- **Admin endpoints no longer triple-call** — `/admin/logs` was calling `get_recent_logs` 3 times per request (for `total`, `returned`, and `logs`). Now calls once per filter set, with consistent snapshot.
- **Removed duplicate `import os`** in `server.py` entrypoint.
- **Refactored `make_tool_call_objects` / `make_tool_call_delta`** — 90% duplicated code consolidated into `_tool_call_to_openai` helper.
- **Cleaned up unreadable logging call** — `upstream.post.start` log event had nested ternaries with triple `payload.get("data", ...)` calls. Now extracts fields once into readable local variables.
- **Removed `record.to_dict()["duration_ms"]`** — was building a full 25-key dict every request just to read one field. Now computes `duration_ms` directly.
- **README deduplicated** — removed duplicate "Run with Docker locally" section.

### 📚 Documentation

- Comprehensive README rewrite with feature list, quick start, usage examples (SDK + curl + CLI), configuration reference, API endpoints table, deployment guide, and architecture diagram.
- Added `LICENSE` (MIT).
- Added `CHANGELOG.md` (this file).

## [1.2.0] — 2025 (original)

Initial public release with OpenAI-compatible API, streaming, thinking mode, tool calling, concurrency limiter, admin endpoints, and `hy3.sh` CLI.
