# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [1.5.1] — 2026-08-06

Bug-fix release addressing 24 issues found in a v1.5.0 code review. All changes
are backward-compatible — no breaking API changes. Existing API consumers
(Kilocode, Opencode, OpenAI Python SDK, etc.) continue to work without
modification.

### 🔴 Critical (API contract violations)

- **#5: `/v1/responses` now rejects `stream=true` with 422.** The endpoint
  accepted `stream` and its docstring advertised Responses API stream events
  (`response.created`, `response.output_text.delta`, `response.completed`),
  but the implementation always collected the full response and returned a
  single JSON object. Clients requesting streaming would hang waiting for
  events that never arrive. Now returns a clear 422 validation error pointing
  to `/v1/chat/completions` for streaming.
- **#7: `_content_to_str()` no longer crashes on non-string `text` values.**
  The function assumed `text` was always a string, but the OpenAI spec allows
  any JSON value. A part like `{"type": "text", "text": 123}` or `{"type":
  "text", "text": null}` would raise `TypeError` in `"\n".join(parts)`,
  turning a malformed-client 400 into a 500. Now coerces non-string values
  (None → "", numbers/bools → stringified).
- **#9: Upstream returning no usable chunks now returns 502.** If the Hy3
  upstream stream closed prematurely, contained only malformed SSE events,
  or emitted an error in another SSE field, the server would return HTTP 200
  with empty content. Now tracks whether at least one valid snapshot was
  received (in both streaming and non-streaming paths) and returns 502 with
  a clear "upstream returned no usable snapshot" error if not.
- **#11: `/v1/responses` now implements `tool_choice`.** The field was
  accepted and copied into an unused `ChatCompletionRequest`, but the actual
  payload never applied the tool-choice prompt prefix or stripped tools for
  `tool_choice="none"`. Now reuses the same WP4 helpers as
  `/v1/chat/completions`.
- **#12: `/v1/responses` function-call IDs are now consistent.** `id` and
  `call_id` were each calling `uuid.uuid4()` separately, producing different
  values. Clients use `call_id` to associate tool results with the
  originating function call — different values broke that correlation. Now
  generates the ID once and reuses it for both fields.
- **#13: `/v1/responses` no longer discards text when tool calls are present.**
  The endpoint used an `if/else` that emitted either function-call items OR
  a text message, never both. If the model returned explanatory text AND
  tool calls, the text was silently dropped. Now emits both in the `output`
  array (reasoning + function_calls + text message), matching the Responses
  API spec.
- **#14: Removed false `response_format` validation/retry claims.** The
  implementation only injects prompt instructions — there was no
  `json.loads()` validation, no JSON Schema validation, and no retry.
  Comments and the v1.5.0 CHANGELOG explicitly claimed "post-stream JSON
  validation retries once," which was false. Now documented honestly as
  best-effort JSON prompting. True structured-output enforcement requires
  upstream Hy3 support, which does not exist.

### 🟠 High (correctness & observability)

- **#17: Streaming tool-argument divergence is now detected.** Arguments
  were assumed to grow monotonically. If a later cumulative snapshot shrank
  the args or changed already-emitted characters (regeneration, retry), the
  server silently kept stale data. Now detects shrinkage and emits a stream
  error event (`upstream_tool_args_diverged`) so the client knows to retry.
- **#18: Text divergence no longer emits duplicate content.** When a new
  snapshot did not start with the previous one, the previous behavior was to
  reset `last_resp_len=0` and re-emit the new snapshot from the start —
  but clients cannot retract already-emitted text, so this produced
  concatenated/duplicated output. Now emits a stream error event
  (`upstream_snapshot_diverged`) instead.
- **#19: Non-streaming cancellation now finalizes request records.**
  `asyncio.CancelledError` derives from `BaseException`, not `Exception`,
  so the surrounding `except Exception` clause didn't catch it. Disconnected
  non-streaming requests vanished from `/admin/requests`. Now explicitly
  catches `CancelledError`, stamps status 499 (nginx convention), and
  finalizes the record.
- **#20: `messages_to_hy3()` now actually finds the last user message.**
  When the final message was `assistant`, the previous behavior made the
  assistant's text the new top-level prompt (`tail or "Continue."`), which
  conflicted with the v1.4.0 changelog claim that the code "correctly finds
  the last user message." Now scans backwards to find the latest user
  message and uses it as the prompt. Messages after it (assistant turn,
  tool result) become part of history.
- **#23: Split `/health` (liveness) from `/ready` (readiness).** The
  previous `/health` returned 503 when the upstream error rate was high,
  which is a reasonable readiness signal but a dangerous liveness signal —
  orchestrators would restart the proxy during an upstream outage,
  destroying diagnostics and adding recovery churn. Now:
  - `/health` always returns 200 while the process is alive (pure liveness).
  - `/ready` returns 503 when the upstream error rate is high (readiness).
  - `Dockerfile` HEALTHCHECK and `render.yaml` `healthCheckPath` updated to
    use `/ready`.

### 🟡 Medium (input validation & DoS hardening)

- **#25: `stop` input is now strictly validated.** Previously, non-string
  list entries, empty strings, and lists longer than 4 entries were silently
  dropped or truncated. Now returns 422 with a clear validation error
  message for each case.
- **#26: `temperature` and `top_p` are now bounded.** `temperature` is
  limited to `[0, 2]` and `top_p` to `[0, 1]` per the OpenAI spec. Values
  outside these ranges return 422 instead of being forwarded to upstream
  Hy3 (which has inconsistent behavior for out-of-range values).
- **#27: `developer` messages are now treated as system instructions.**
  The `developer` role was accepted by the Pydantic model but routed to
  history in `messages_to_hy3()`, even though OpenAI's spec defines
  `developer` as the newer name for system-prompt-style instructions. Now
  concatenated into `system_prompt` alongside `system` messages.
- **#28: Message content limits now include significant request fields.**
  `MAX_CONTENT_CHARS` previously counted only `message.content`. Now also
  counts `tool_calls`, `reasoning_content`, `name`, and `tool_call_id` on
  each message. Closes a DoS gap where a request with tiny messages but a
  5MB `tools` array would pass the limit (the `tools` array at the request
  level is still counted separately in the usage estimation).
- **#29: SSE buffer cap is now measured in bytes.** `len(line)` counts
  Python Unicode characters, but `SSE_BUFFER_CAP` and its comment both say
  "bytes." A line of 10M emoji chars is ~40MB in UTF-8 but only 10M in
  chars — the char-based check would have let it through. Now uses
  `len(line.encode("utf-8", errors="replace"))`.

### 🟢 Low (dead code & CLI robustness)

- **#35: Removed dead `chat_req` construction in `/v1/responses`.** A
  `ChatCompletionRequest` was built but never used, providing only
  incidental secondary validation. Removed; validation is now intentional
  via `ResponsesRequest`'s own validators.
- **#36: `hy3.sh` file-based tools are now validated.** The `-f` flag
  accepted a readable file without parsing its contents. Invalid JSON would
  then crash `build_payload` with a Python traceback. Now validates with
  `json.loads` before accepting.
- **#37: `hy3.sh` corrupt conversation state no longer causes a traceback.**
  Saved history was read directly and passed to `json.loads` without a
  user-friendly validation path. A partially written or manually damaged
  state file terminated the CLI with a Python traceback. Now validates and
  prints a clear error with instructions to delete the file.
- **#38: `hy3.sh` SSE shape check is now complete.** The check verified
  `data[0]` is a list but not that it contains an element. A payload shaped
  as `[[]]` would pass the check, then crash on `data[0][0]` with
  `IndexError` — outside the try/except block. Now verifies
  `len(data[0]) >= 1` and safely coerces `None`/non-string values.
- **#39: `hy3.sh` tool-call parsing no longer assumes every item is a dict.**
  `tc.get(...)` was called without verifying `tc` is a dict. Malformed
  upstream tool-call data (e.g. a bare string) would crash the CLI with
  `AttributeError` after an otherwise successful generation. Now checks
  `isinstance(tc, dict)` and skips malformed entries with a warning.
- **#40: `hy3.sh` usage errors now exit with status 1.** `usage()` always
  exited 0, including for invalid options and missing prompt. Automation
  could not distinguish invalid invocation from success. Now split into
  `print_usage` (exits 0 for `-h`) and `usage_error` (exits 1 for errors).
- **#41: `hy3.sh` raw mode no longer silently succeeds with no result.**
  If SSE data existed but no valid payload was parsed, raw mode emitted
  nothing and exited 0. Now prints an error to stderr and exits 1.

### ⚙️ Internal

- Version bumped from 1.5.0 to 1.5.1. Single source of truth (`__version__`
  constant in `server.py`) reflects the new version; `pyproject.toml` is
  in sync.
- `Dockerfile` HEALTHCHECK and `render.yaml` `healthCheckPath` updated to
  use `/ready` instead of `/health`.
- Root endpoint (`GET /`) now advertises both `/health` and `/ready`.

## [1.5.0] — 2026-08-06

Compatibility release targeting **Kilocode** (kilo.ai) and **Opencode** (opencode.ai).
Both tools use the Vercel AI SDK `@ai-sdk/openai-compatible` package as their HTTP
client, which enforces a stricter subset of the OpenAI specification than the
official Python SDK. This release closes the three critical gaps that prevented
those tools from using Hy3, plus several secondary spec-coverage improvements.

All changes are additive and backward-compatible — existing API consumers
(Hermes Agent, Agent Zero, OpenClaw, and any other client built on the official
OpenAI SDK) continue to work without modification.

### 🔴 Critical

- **WP1: Streaming usage support.** When `stream_options.include_usage` is true,
  the server now emits a final SSE chunk with the populated `usage` object before
  `data: [DONE]`. The chunk has an empty `choices` array (present but empty, not
  omitted) and a fully-populated `usage` object with `prompt_tokens`,
  `completion_tokens`, and `total_tokens` fields. Required by the Vercel AI SDK
  `@ai-sdk/openai-compatible` package's `includeUsage` provider setting. Fixes
  always-zero token usage reported by Kilocode and Opencode
  ([anomalyco/opencode#423](https://github.com/anomalyco/opencode/issues/423),
  [vercel/ai#6774](https://github.com/vercel/ai/issues/6774)).
- **WP2: `/v1/models` metadata enrichment.** Each model entry now includes
  `context_length`, `max_tokens`, `tool_call`, `reasoning`,
  `supports_parallel_tool_calls`, and `supports_structured_outputs` fields.
  Enables auto-compaction in both tools without manual `limit.context`/
  `limit.output` configuration. Without these fields, Kilocode's documentation
  explicitly states "compaction is disabled" and "conversations will grow
  unbounded until the provider rejects the request."
- **WP3: Tool-call streaming delta conformance.** Tool-call deltas are now
  emitted incrementally across the SSE stream, with each delta including the
  `index` field identifying which tool call the delta applies to. The first
  delta for an index carries `id` + `function.name` + empty `function.arguments`;
  subsequent deltas for the same index carry `function.arguments` fragments.
  Matches the OpenAI streaming specification. Fixes null-arguments tool calls
  in Kilocode and Opencode caused by the v1.4.5 single-end-of-stream delta
  emission pattern.

### 🟠 High

- **WP4: `stop` sequences.** When `stop` is set (string or array of up to 4
  strings), the streaming and non-streaming paths both truncate the response at
  the first occurrence of any stop sequence (excluding the sequence itself from
  emitted text). Implemented client-side because Hy3 upstream does not natively
  support stop sequences. `finish_reason` is set to `"stop"`.
- **WP4: `response_format`.** `json_object` mode prepends an instruction to the
  system prompt directing the model to produce valid JSON. `json_schema` mode
  additionally prepends the JSON schema. True constrained decoding is not
  possible without upstream support; this prompt-prefix approach matches what
  most OpenAI-compatible proxies do.
- **WP4: `tool_choice`.** Three modes honored: `"none"` (strips `tools` from
  upstream payload and prepends a no-tools instruction), `"auto"` (default,
  no-op), and `{"type": "function", "function": {"name": "..."}}` (prepends a
  forced-function instruction). `"required"` is also accepted and translated to
  a prompt-level instruction.
- **WP4: `parallel_tool_calls`.** When `false`, prepends a prompt instruction
  limiting the model to a single tool call per response. When `true` or
  omitted, no action is taken (Hy3's default behavior already allows multiple
  tool calls).

### 🟡 Medium

- **WP5: `system_fingerprint` field.** Added to all non-streaming and streaming
  responses. Value is `fp_hy3_<version>` (e.g. `fp_hy3_1.5.0`). Identifies the
  exact server build that produced a response; clients use it for caching and
  reproducibility tracking.
- **WP5: `logprobs: null` in choices.** Added to every `choices` entry in
  non-streaming responses and to every SSE chunk. Hy3 doesn't support logprobs;
  the field is always `null`. Strict OpenAI parsers expect the field to be
  present even when null.
- **WP5: dual `reasoning` field.** The `reasoning_content` field (OpenAI o1-style,
  original to Hy3 v1.4.x) is preserved, and a parallel `reasoning` field is now
  emitted alongside it in streaming deltas and non-streaming messages. The
  Vercel AI SDK package looks for the `reasoning` field name; emitting both
  maximizes client compatibility.

### 🟢 Low / Optional

- **WP6: `/v1/responses` endpoint.** Optional minimal OpenAI Responses API
  endpoint for tools that prefer `@ai-sdk/openai` (the OpenAI official SDK
  package) over `@ai-sdk/openai-compatible`. Translates the Responses API
  request shape (`input` instead of `messages`) to the existing Chat Completions
  internals, then translates the response back to the Responses API shape
  (`output` array with `message`/`function_call`/`reasoning` items, `usage`
  with `input_tokens`/`output_tokens`). Both Kilocode and Opencode default to
  `@ai-sdk/openai-compatible`, so this endpoint is optional.

### 📚 Documentation

- **New `TOOLS.md`** with Kilocode and Opencode setup guides, configuration
  reference tables, verification checklists, and troubleshooting sections.
- **New `examples/` directory** with `kilo.json.example` and
  `opencode.json.example` — ready-to-paste config snippets for both local
  development and production deployment.
- **README updated** with a Compatibility section listing the tools Hy3 has
  been tested with.

### ⚙️ Internal

- Version bumped from 1.4.5 to 1.5.0. Single source of truth (`__version__`
  constant in `server.py`) reflects the new version; `pyproject.toml` is in
  sync.
- New `StreamOptions` and `ResponseFormat` Pydantic models added to support
  the new request fields.
- New `make_usage_chunk` helper added alongside the existing `make_chunk` for
  the WP1 streaming usage chunk.
- New `_normalize_stop_sequences`, `_truncate_at_stop`,
  `_build_response_format_prefix`, `_build_tool_choice_prefix`, and
  `_build_parallel_tools_prefix` helpers added for WP4.
- The `stream_openai` async generator signature gained four new optional
  parameters: `stream_options`, `stop_sequences`, `req_messages`, `req_tools`.
  All default to `None` and are backward-compatible with existing callers.
- New `HY3_MODELS` module-level constant enumerates the two served models with
  their full metadata, used by the enriched `/v1/models` endpoint.
- New `SYSTEM_FINGERPRINT` module-level constant derived from `__version__`.

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
