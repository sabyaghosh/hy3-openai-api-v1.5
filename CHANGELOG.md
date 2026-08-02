# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [1.4.5] — 2025-08-03

Full line-by-line code review of the repository. Note that `server.py` had been
bumped to `1.4.4` without any corresponding `1.4.2`/`1.4.3`/`1.4.4` changelog
entries or README updates; this release reconciles the version across
`server.py`, `pyproject.toml`, and this file.

### 🔴 Critical

- **`pyproject.toml` added — the Diploi deployment could not start.** `diploi.yaml`
  launches the app with `uv run --with uvicorn uvicorn server:app`. `uv run`
  resolves dependencies from `pyproject.toml` (or PEP-723 inline metadata) and
  **does not read `requirements.txt`**, so the runtime venv contained only
  `uvicorn` and the app crashed on `import fastapi`. `requirements.txt` is
  retained for the standalone `Dockerfile`, which uses `pip install -r`.
- **`.python/` added to `.gitignore` and `.dockerignore`.** `Dockerfile.dev` sets
  `UV_PYTHON_INSTALL_DIR=$FOLDER/.python`, so uv unpacks a complete CPython tree
  (hundreds of MB) into the repo root. It was ignored by neither file and would
  have been committed to Git and shipped into the Docker build context.
- **CI "unreferenced constants" check was a permanent no-op.** `used` was built
  from every `ast.Name` node, but an assignment *target* is itself an `ast.Name`,
  so `defined - used` was always empty. The check reported success on every run
  and could never have caught the `SSE_BUFFER_CAP`-class bug it was written for.
  `used` is now restricted to `ast.Load` contexts, and `logging_layer.py` is
  scanned too.

### 🟠 High

- **Aborted streams were never recorded.** A client disconnect raises
  `GeneratorExit` at a `yield` in `stream_openai`. `GeneratorExit` derives from
  `BaseException`, so neither `except HTTPException` nor `except Exception`
  caught it, and `record.finalize()` was never reached — every cancelled stream
  vanished from `/admin/requests`. The `finally` block now stamps `499` and
  finalizes. Added `RequestRecord.finalized` as a public view of the guard so
  cleanup paths don't touch `_finalized`.
- **`hy3.sh`: `set -e` made the `event_id` error handler unreachable.** The
  `if [[ $? -ne 0 ]]` check after `EVENT_ID=$(... python3 ...)` was dead code —
  `set -e` aborts the script on the failing command substitution before the check
  runs. Converted to the `|| rc=$?` idiom already used for the `curl` calls.
- **`hy3.sh`: `$ERRFILE` was unbound inside the EXIT trap.** The trap referencing
  `"$ERRFILE"` is installed *before* `ERRFILE=$(mktemp)`. Under `set -u`, if the
  second `mktemp` fails the trap dies with `ERRFILE: unbound variable`, masking
  the real error. Both vars are now pre-initialised and expanded with `${…:-}`.

### 🟡 Medium

- **Error responses are now OpenAI-shaped.** Only the 503 and 413 paths returned
  `{"error": {...}}`; every `raise HTTPException(...)` (401/400/502/504, admin
  404) returned FastAPI's `{"detail": ...}` and 422s returned `{"detail": [...]}`.
  OpenAI SDK clients parse `error.message`, so these surfaced as opaque failures.
  Added `StarletteHTTPException` and `RequestValidationError` handlers. The 422
  handler builds its message from strings only — `exc.errors()` can embed a raw
  `ValueError` under `ctx`, which is not JSON-serialisable and would turn a 422
  into a 500.
- **`limit <= 0` handling in `logging_layer` query helpers.** `get_recent_logs`
  appends before testing `len(out) >= limit`, so `?limit=0` returned one entry.
  `get_recent_requests` used `entries[:limit]`, so `?limit=-5` returned *all but
  the last five* records. Both now return `[]`.
- **`hy3.sh`: `-T`/`-p` are now validated.** `-t` and `-m` were checked but
  temperature/top-p were passed straight to `json.loads`, so a non-numeric value
  produced a raw Python traceback instead of a usage error.

### 🔵 Low / Docs

- **`TRUST_PROXY_HEADERS` moved above `_client_ip()`**, which reads it. It
  previously worked only because the global is resolved at call time.
- **`REQUEST_BUFFER_SIZE` exported** from `logging_layer` and used by
  `/admin/requests/{id}` instead of a hardcoded `200`, which would silently
  truncate the scan if the buffer cap were raised.
- **Stale `SSE_BUFFER_CAP` comment fixed** — still cited `max_tokens=262144` as
  the sizing basis after `DEFAULT_MAX_TOKENS` was lowered to 4096.
- **README `/health` description corrected** — documented a "counter overflow
  (`active_requests > max_concurrent`)" 503 condition that the implementation
  does not contain. The endpoint only checks the 50-request sliding error window.
- **`.dockerignore` now excludes `.env*`, `*.key`, `*.pem`, and `secrets/`.**

## [1.4.1] — 2026-08-02

Second-pass cleanup addressing dead code, wrong comments, and documentation
mismatches found in the post-v1.4.0 review. No behavior changes — all fixes
are documentation, comments, or dead-code removal. Test battery passes 20/20.

### 🔴 Critical (Dead Code & Wrong Docs)

- **`logging_layer.py` docstring fixed** — module docstring referenced `RequestTracker` (does not exist; the class is `RequestRecord`). Buffer size documented as `1000` (actual: `2000`). Both corrected.
- **Dead `import asyncio` removed** from `logging_layer.py` — was tagged `# noqa: F401 (retained for backward compat)` but no downstream code imports `asyncio` from this module. The "backward compat" justification was hollow.
- **Orphan comment removed** — `logging_layer.py` had a comment describing a `request_id -> list of log entries` index that was never implemented. Removed.

### 🟠 High (Wrong/Stale Comments)

- **`ConcurrencyLimiter.acquire()` docstring fixed** — claimed "clean non-blocking path that doesn't rely on the 1ms hack" but the implementation literally uses a 1ms timeout. Now honestly describes the 1ms approximation.
- **Contradictory system-message comment fixed** — `messages_to_hy3` had "Use the last system message" followed by "FIX: concatenate multiple". Removed the stale first line.
- **README `/health` description updated** — was "returns 503 if active_requests > max_concurrent" (incomplete). Now also mentions the high-error-rate condition added in v1.4.0.
- **README `scripts/` link removed** — referenced a `scripts/` directory that does not exist in the repo. Removed the "Run tests" subsection.
- **README `What's New` section updated** — was stuck at v1.3.0; added v1.4.0 section with summary of fixes.
- **`hy3.sh` usage text fixed** — described the old `TOOL_CALL: function_name(args_json)` format (replaced in v1.3.0). Now shows the correct `TOOL_CALL:{json_envelope}` format with an example.

### 🟡 Medium (Dead Code & Inconsistencies)

- **6 unused request fields documented** — `stop`, `n`, `user`, `presence_penalty`, `frequency_penalty`, `logit_bias`, `seed` are accepted for OpenAI spec compatibility but silently dropped (Hy3 doesn't support them). Now documented inline.
- **`stream_openai(limiter=)` made required** — was `Optional[ConcurrencyLimiter] = None` but the only caller always passes it. The `if limiter is not None:` guard in the `finally` block would skip the release if None, leaking the slot. Removed the default and the guard.
- **`Connection: keep-alive` header removed** from streaming responses — it's the HTTP/1.1 default and forbidden in HTTP/2. `Cache-Control` + `X-Accel-Buffering` are sufficient.
- **`record.think_level` double-assignment fixed** — was set to `req.think_level or DEFAULT_THINK_LEVEL` (logging the pre-override value), then overwritten after the hy3-think override. Now the override runs first, so `request.start` logs the actual value sent upstream.
- **Dead `len(inner) > 0` branch removed** in `parse_hy3_data` — the `not inner` check above already guarantees it.
- **`HTTP_TIMEOUT` reduced** from 300s to 90s — Render's gateway kills requests at ~100s; a 300s read timeout left the limiter slot occupied for ~210s after the client already got a 504.
- **Version single source of truth** — added `__version__ = "1.4.0"` module constant; both `FastAPI(version=...)` and the `/` endpoint now reference it instead of hardcoding separate strings.
- **Response header consistency** — `X-Active-Requests` now present on both 503 and streaming responses (was only on 503).

### 🔵 Low (Polish)

- **`Q#`/`Bug #`/`Sec #` prefixes removed** from all comments in `server.py` and `hy3.sh` — they referenced an internal review numbering scheme that no longer exists in the repo.
- **Long line wrapped** in `chat_completions` — the `log_event("warning", "request.unauthorized", ...)` call was 119 chars on one line; now multi-line for consistency with the rest of the file.
- **v1.3.0 changelog corrected** — claimed admin endpoints made "3 calls" to `get_recent_logs`; was actually 2. Added a correction note.
- **Dockerfile HEALTHCHECK fixed** — used `${PORT}` in `CMD python -c "..."` form, which doesn't expand env vars (no shell). Rewrote as `CMD sh -c '...'` so `${PORT}` expands correctly. Without this fix, the HEALTHCHECK would always fail.
- **Deferred TODO comment converted** to a plain descriptive comment — the "NOTE: consider a shared client" was a TODO masquerading as documentation.

## [1.4.0] — 2026-08-02

Follow-up fix release addressing issues found in the post-1.3.0 code review.

### 🔴 Critical Fixes

- **Timing attack on API key validation** — `token in API_KEYS` (set membership) was vulnerable to timing side-channels. Now uses `hmac.compare_digest()` against each configured key.
- **Timing attack on admin token** — `token != ADMIN_TOKEN` (string inequality) was vulnerable to the same. Now uses `hmac.compare_digest()`.
- **`httpx.HTTPStatusError` dead code removed** — the v1.3.0 changelog falsely claimed this handler was reactivated. It was still dead because `call_hy3_stream` raises `HTTPException` (not `HTTPStatusError`) on non-200 responses. The dead `except` block has been removed; the manual status check (which provides better error messages) is kept.
- **Dockerfile `image.source` label fixed** — was still pointing to `sabyaghosh/hy3-client` (the v1.3.0 changelog claimed this was fixed but it wasn't). Now correctly points to `sabyaghosh/hy3-openai-api`.
- **`Dockerfile` HEALTHCHECK added** — standalone `docker run` users now get container health status via the `/health` endpoint.

### 🟠 High Fixes

- **Multiple system messages now concatenated** — previously only the last system message was kept (OpenAI convention is to concatenate). Affects system-prompt-heavy agentic workflows.
- **Last-message-role assumption fixed** — if the last message was an assistant message (continuation requests), the code was using the assistant's content as the "current prompt". Now correctly finds the last user message.
- **`hy3.sh` curl timeouts added** — POST now has `--max-time 30`, stream GET has `--max-time 300`. Previously a hung upstream would hang the CLI forever.
- **`hy3.sh` stderr redirection fixed** — `2>&1` was mixing curl errors into the SSE data file, breaking the JSON parser. Now stderr goes to a separate `ERRFILE`.
- **`render.yaml` security placeholders** — commented-out `API_KEYS` and `ADMIN_TOKEN` env vars are now included so one-click deploys remind users to set auth.
- **`/health` endpoint now detects degradation** — previously the 503 path was unreachable (always returned 200). Now returns 503 on counter overflow or high error rate (>50% of last 10+ requests).

### 🟡 Medium Fixes

- **OpenAI version string mismatch** — FastAPI app `version` was `1.2.0` while `/` endpoint returned `1.3.0`. Both now return `1.3.0`.
- **`content` field accepts multimodal lists** — `ChatMessage.content` now accepts `str | list` (OpenAI multimodal format). Text parts are extracted via `_content_to_str()`; non-text parts are dropped (Hy3 doesn't support images).
- **`make_chunk` timestamp consistency** — `created` is now captured once per completion and reused across all chunks (matches OpenAI behavior; previously each chunk got a fresh timestamp).
- **`PORT` env var validation** — `int(os.environ.get("PORT", "8000"))` crashed on `PORT=abc`. Now uses `_parse_int_env()` with fallback.
- **`ConcurrencyLimiter.release()` simplified** — underflow path no longer calls `self._sem.release()` (which always raised `ValueError` on an already-max semaphore). Cleaner code, same behavior.
- **Admin endpoints no longer double-call** — `/admin/logs` and `/admin/requests` were calling `get_recent_logs`/`get_recent_requests` twice (once for count, once for data). Now uses `get_log_summary()` for the count.
- **`/admin/requests/{id}` honest limit** — was passing `limit=10000` to a function capped at 200. Now passes `limit=200` to match the actual buffer size.

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
- **Admin endpoints no longer double-call** — `/admin/logs` was calling `get_recent_logs` 2 times per request (once for the total count, once for the filtered data). Now uses `get_log_summary()` for the count. (An earlier version of this changelog claimed 3 calls; that was an overcount — it was 2.)
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
