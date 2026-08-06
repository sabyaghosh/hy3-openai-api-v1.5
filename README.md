# Hy3 OpenAI-Compatible API

A production-ready OpenAI-compatible API proxy for [Tencent Hy3](https://huggingface.co/spaces/tencent/Hy3) — the 295B MoE model — via HuggingFace's Gradio API. **No API key required** to call Hy3 upstream. Drop-in replacement for `https://api.openai.com/v1`.

[![Deploy to Render](https://render.com/images/deploy-to-render-button.svg)](https://render.com/deploy?repo=https://github.com/sabyaghosh/hy3-openai-api-final)
[![Python 3.11+](https://img.shields.io/badge/python-3.11+-blue.svg)](https://www.python.org/downloads/)
[![FastAPI](https://img.shields.io/badge/FastAPI-0.110%2B-009688.svg)](https://fastapi.tiangolo.com/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

---

## ✨ Features

- **OpenAI-compatible** — point any OpenAI SDK / LangChain / LlamaIndex client at `/v1`
- **Streaming + non-streaming** — full SSE support with `text/event-stream`
- **Streaming usage** — `stream_options.include_usage` emits a final usage chunk before `[DONE]` (required by Vercel AI SDK `@ai-sdk/openai-compatible`)
- **Thinking mode** — exposes model reasoning via both `reasoning_content` (o1-style) and `reasoning` (AI SDK-style)
- **Tool calling** — full OpenAI function-calling format with incremental streaming deltas
- **OpenAI spec coverage** — `stop` sequences, `response_format` (json_object / json_schema), `tool_choice` (none / auto / required / forced), `parallel_tool_calls`, `system_fingerprint`, `logprobs`
- **Multi-turn conversations** — pass full message history
- **Concurrency control** — semaphore with queue + 503 + `Retry-After` (prevents upstream overload)
- **Observability** — structured logging, in-memory ring buffer, admin endpoints, `/stats` dashboard
- **Production hardening** — optional API keys, admin token auth, input size limits, CORS spec compliance, graceful error handling
- **CLI client** (`hy3.sh`) — quick one-shot prompts from the terminal
- **Docker-ready** — one-command deploy to Render / Fly.io / Cloud Run / Koyeb / Railway / HF Spaces

### 🤝 Tool Compatibility

Hy3 v1.5.0+ is tested and known to work with:

| Tool | Status | Setup guide |
|---|---|---|
| **Kilocode** (kilo.ai) | ✅ v1.5.0+ | [`TOOLS.md`](TOOLS.md) · [`examples/kilo.json.example`](examples/kilo.json.example) |
| **Opencode** (opencode.ai) | ✅ v1.5.0+ | [`TOOLS.md`](TOOLS.md) · [`examples/opencode.json.example`](examples/opencode.json.example) |
| **Hermes Agent** | ✅ all versions | Official OpenAI SDK — works out of the box |
| **Agent Zero** | ✅ all versions | Official OpenAI SDK — works out of the box |
| **OpenClaw** | ✅ all versions | Official OpenAI SDK — works out of the box |
| OpenAI Python SDK | ✅ all versions | Drop-in replacement for `https://api.openai.com/v1` |
| LangChain | ✅ all versions | Use `ChatOpenAI(base_url="http://<host>:<port>/v1", api_key="anything")` |
| LlamaIndex | ✅ all versions | Use `OpenAI(api_base="http://<host>:<port>/v1", api_key="anything")` |

For setup walkthroughs, configuration references, and troubleshooting, see [`TOOLS.md`](TOOLS.md).

---

## 📦 What's New

### v1.5.1 (Bug-Fix Release)

Addresses 24 issues found in a v1.5.0 code review. All changes are backward-compatible — no breaking API changes. Existing API consumers (Kilocode, Opencode, OpenAI Python SDK, etc.) continue to work without modification.

**Critical fixes (API contract violations):**

- **#5**: `/v1/responses` now rejects `stream=true` with 422 (was silently ignored, leaving clients waiting for events that never arrive)
- **#7**: `_content_to_str()` no longer crashes on non-string `text` values (was 500 instead of 422)
- **#9**: Upstream returning no usable chunks now returns 502 (was misleading 200 with empty content)
- **#11**: `/v1/responses` now implements `tool_choice` (was accepted but unused)
- **#12**: `/v1/responses` function-call `id` and `call_id` are now consistent (were different UUIDs, breaking tool-result correlation)
- **#13**: `/v1/responses` no longer discards text when tool calls are present (now emits both)
- **#14**: Removed false `response_format` validation/retry claims (was best-effort prompting only, no validation)

**High-severity fixes (correctness & observability):**

- **#17**: Streaming tool-argument divergence now detected and emits stream error (was silently stale)
- **#18**: Text divergence emits stream error instead of duplicating content
- **#19**: Non-streaming cancellation now finalizes request records (was vanishing from `/admin/requests`)
- **#20**: `messages_to_hy3()` now actually finds the last user message (was using assistant text as prompt)
- **#23**: Split `/health` (liveness, always 200) from `/ready` (readiness, 503 on upstream issues); updated Dockerfile + render.yaml

**Medium-severity fixes (input validation & DoS hardening):**

- **#25**: `stop` input strictly validated (422 on bad input, not silent repair)
- **#26**: `temperature` bounded to `[0, 2]`, `top_p` bounded to `[0, 1]`
- **#27**: `developer` messages now treated as system instructions (was routed to history)
- **#28**: Message content limits now include `tool_calls`, `reasoning_content`, `name`, `tool_call_id`
- **#29**: SSE buffer cap now measured in bytes (was chars — Unicode-heavy lines could bypass)

**Low-severity fixes (dead code & CLI robustness):**

- **#35**: Removed dead `chat_req` construction in `/v1/responses`
- **#36–41**: `hy3.sh` CLI fixes — file-based tools validation, corrupt history handling, SSE shape check, tool-call dict verification, usage error exit codes, raw mode empty-result error

See [CHANGELOG.md](CHANGELOG.md) for the full list with detailed explanations.

### v1.5.0 (Kilocode + Opencode Compatibility Release)

This release closes the three critical gaps that prevented **Kilocode** (kilo.ai) and **Opencode** (opencode.ai) from using Hy3 as a backend. Both tools build on the Vercel AI SDK [`@ai-sdk/openai-compatible`](https://ai-sdk.dev/providers/openai-compatible-providers) package, which enforces a stricter subset of the OpenAI specification than the official Python SDK.

**Critical fixes:**

- **WP1 — Streaming usage support.** When `stream_options.include_usage` is true, the server now emits a final SSE chunk with the populated `usage` object before `data: [DONE]`. Fixes always-zero token usage reported by both tools.
- **WP2 — `/v1/models` metadata enrichment.** Each model entry now includes `context_length`, `max_tokens`, `tool_call`, `reasoning`, `supports_parallel_tool_calls`, and `supports_structured_outputs` fields. Enables auto-compaction in both tools without manual `limit.context`/`limit.output` configuration.
- **WP3 — Tool-call streaming delta conformance.** Tool-call deltas are now emitted incrementally with the `index` field on each delta, matching the OpenAI streaming spec. Fixes null-arguments tool calls in both tools.

**High-severity additions:**

- **WP4 — OpenAI spec coverage.** `stop` sequences (client-side truncation), `response_format` (`json_object` / `json_schema`), `tool_choice` (`none` / `auto` / `required` / forced function), and `parallel_tool_calls` (prompt-level enforcement when `false`).
- **WP5 — Polish fields.** `system_fingerprint`, `logprobs: null` in choices, dual `reasoning` / `reasoning_content` field emission for AI SDK compatibility.
- **WP6 — `/v1/responses` endpoint (optional).** Minimal OpenAI Responses API adapter for tools that prefer `@ai-sdk/openai` over `@ai-sdk/openai-compatible`.
- **WP7 — Tool config snippets + `TOOLS.md`.** New `examples/kilo.json.example` and `examples/opencode.json.example` plus a full setup guide in [`TOOLS.md`](TOOLS.md).

All changes are additive and backward-compatible — existing API consumers (Hermes Agent, Agent Zero, OpenClaw, etc.) continue to work without modification. See [CHANGELOG.md](CHANGELOG.md) for the full list.

### v1.4.5 (Code-Review Release)

Full line-by-line review of the repo. Highlights:

- **`pyproject.toml` added — fixes a startup crash on Diploi.** `diploi.yaml` runs
  `uv run --with uvicorn uvicorn server:app`, and `uv run` resolves deps from
  `pyproject.toml`, **not** `requirements.txt`. Without it the venv contained only
  `uvicorn` and the app died on `import fastapi`.
- **`.python/` is now git/docker-ignored** — `Dockerfile.dev` unpacks a full CPython
  tree into the repo root via `UV_PYTHON_INSTALL_DIR`
- **CI unused-constant check actually works now** — it compared against every
  `ast.Name` including assignment targets, so it could never report anything
- **Cancelled streams are recorded** — `GeneratorExit` on client disconnect bypassed
  both `except` clauses, so aborted streams never reached `/admin/requests`
- **All errors use the OpenAI `{"error": {...}}` envelope** — previously only 503/413 did
- **`hy3.sh`**: unreachable `event_id` error handler under `set -e`, unbound `$ERRFILE`
  in the EXIT trap, and unvalidated `-T`/`-p` flags
- **`/admin/*` `limit=0` / negative `limit`** no longer return bogus result sets
- See [CHANGELOG.md](CHANGELOG.md) for the full list

### v1.4.0 (Review-Fix Release)

Follow-up fix release addressing issues found in a post-1.3.0 code review:

- **Timing-safe auth** — API key and admin token validation now use `hmac.compare_digest()` to prevent timing side-channel attacks
- **Multimodal content** — `messages[].content` now accepts `str | list` (OpenAI multimodal format); text parts are extracted, non-text parts dropped
- **Multiple system messages** — concatenated (OpenAI convention) instead of silently overwritten
- **`/health` detects degradation** — returns 503 on counter overflow or high upstream error rate (>50% of last 10+ requests)
- **`hy3.sh` curl timeouts** — POST (30s) and stream GET (300s) no longer hang forever on unresponsive upstream
- **`hy3.sh` stderr separation** — curl errors no longer mixed into SSE data (was breaking the JSON parser)
- **Dockerfile HEALTHCHECK** — standalone `docker run` users now get container health status
- **`render.yaml` security placeholders** — commented-out `API_KEYS`/`ADMIN_TOKEN` remind users to set auth
- **Version single source of truth** — `__version__` constant shared between FastAPI app and `/` endpoint
- **Dead `httpx.HTTPStatusError` handler removed** — v1.3.0 changelog falsely claimed it was reactivated
- See [CHANGELOG.md](CHANGELOG.md) for the full list of 18 fixes

### v1.3.0 (Bug-Fix Release)

This version includes **25 surgical fixes** over the original `hy3-openai-api`. Key improvements:

### Critical Fixes
- **`Dockerfile` now copies `logging_layer.py`** — Docker images were crashing on startup with `ModuleNotFoundError`
- **CORS spec compliance** — `Access-Control-Allow-Origin: *` is no longer paired with `credentials=true` (browsers blocked credentialed requests before)
- **README install URL fixed** — was pointing to a placeholder (`YOUR_USER/hy3-client`)

### Security Hardening
- **Admin endpoints require `ADMIN_TOKEN`** — returns 404 if unset, 401 if wrong token
- **Optional API key validation** — set `API_KEYS=key1,key2` to lock down `/v1/chat/completions`
- **Input size limits** — `MAX_MESSAGES` (default 1000) and `MAX_CONTENT_CHARS` (default 1,000,000) prevent DoS

### Correctness Fixes
- **`ConcurrencyLimiter.release()`** — no longer underflows `active` counter on double-release; invariant `total_acquired == total_completed + total_errors` now holds
- **`RequestRecord.finalize()`** — idempotent (was double-appending to request buffer)
- **Tool-call round-trips** — messages ending with `role=tool` and `content=null` no longer rejected with 400
- **`think_level` override** — explicit `think_level="no_think"` on `hy3-think` model is now respected
- **Token usage** — `usage.prompt_tokens` and `completion_tokens` are now estimated (4 chars ≈ 1 token) instead of hardcoded to 0

### Quality Improvements
- Env var parsing with validation (`MAX_CONCURRENT=abc` no longer crashes startup)
- Pinned dependency upper bounds in `requirements.txt`
- Pinned Python base image in `Dockerfile` (`python:3.11.9-slim-bookworm`)
- `hy3.sh` — curl errors no longer swallowed, file handle leak fixed, bare `except:` replaced with `except Exception:`, path traversal on `-c` ID blocked, `TOOL_CALL` output uses JSON envelope
- Configurable log level via `LOG_LEVEL` env var
- Admin endpoints no longer call `get_recent_logs` 3 times per request

---

## 🚀 Quick Start

### Option A: Run locally with Python

```bash
git clone https://github.com/sabyaghosh/hy3-openai-api.git
cd hy3-openai-api
pip install -r requirements.txt
python server.py --port 8000
```

API now at `http://localhost:8000/v1`. No API key needed — pass any string.

### Option B: Run with Docker

```bash
docker build -t hy3-openai-api .
docker run -p 8000:8000 \
  -e MAX_CONCURRENT=10 \
  -e QUEUE_TIMEOUT=5 \
  hy3-openai-api
```

### Option C: Use the CLI

```bash
curl -sL https://raw.githubusercontent.com/sabyaghosh/hy3-openai-api/main/hy3.sh -o hy3.sh
chmod +x hy3.sh

# Basic prompt
./hy3.sh "explain RSA in 3 sentences"

# Thinking mode
./hy3.sh -t high "prove the halting problem is undecidable"

# Multi-turn conversation
./hy3.sh -c mychat "my name is LO"
./hy3.sh -c mychat "what's my name?"

# Tool calling
./hy3.sh -f tools.json "What is the weather in Tokyo?"
```

---

## 📖 Usage Examples

### OpenAI Python SDK

```python
from openai import OpenAI

client = OpenAI(base_url="http://localhost:8000/v1", api_key="anything")

# Non-streaming
resp = client.chat.completions.create(
    model="hy3",
    messages=[
        {"role": "system", "content": "You are a helpful assistant."},
        {"role": "user", "content": "Explain RSA in 3 sentences."},
    ],
)
print(resp.choices[0].message.content)
print(f"Usage: {resp.usage}")
```

### Streaming with thinking mode

```python
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
```

### Tool calling

```python
resp = client.chat.completions.create(
    model="hy3",
    messages=[{"role": "user", "content": "What's the weather in Tokyo?"}],
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
tc = resp.choices[0].message.tool_calls[0]
print(tc.function.name, tc.function.arguments)
# get_weather {"location": "Tokyo"}
```

### curl

```bash
# Non-streaming
curl -s http://localhost:8000/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{
    "model": "hy3",
    "messages": [{"role": "user", "content": "Hello!"}]
  }' | jq '.choices[0].message.content'

# Streaming (SSE)
curl -N http://localhost:8000/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{
    "model": "hy3-think",
    "messages": [{"role": "user", "content": "Explain quantum entanglement."}],
    "stream": true
  }'
```

Point any OpenAI-compatible client at `http://localhost:8000/v1` with any API key string.

> **Streaming caveats:**
> - **Errors arrive as HTTP 200.** Because the SSE response headers (200) are sent before the upstream call runs, upstream failures are emitted as an SSE `data:` line with an `error` field, followed by `data: [DONE]`. The OpenAI Python SDK may raise a validation error on this shape rather than a clean `APIError`. If you need reliable error detection, use non-streaming requests.
> - **`usage` is absent from streaming responses.** `stream_options: {"include_usage": true}` is not supported (silently ignored). Use non-streaming requests for token counts.

---

## 🔧 Configuration

### Environment Variables

| Variable | Default | Description |
|---|---|---|
| `HOST` | `0.0.0.0` | Bind host |
| `PORT` | `8000` | Bind port (Render injects this automatically) |
| `PYTHONUNBUFFERED` | — | Set to `1` for unbuffered logs |
| `MAX_CONCURRENT` | `10` | Hard cap of in-flight Hy3 calls. Excess requests get queued (see `QUEUE_TIMEOUT`) |
| `QUEUE_TIMEOUT` | `5` | Seconds to wait for a free slot before returning `503` with `Retry-After`. Set to `0` for non-blocking |
| `HTTP_READ_TIMEOUT` | `90` | Upstream read timeout in seconds. Default 90 (below Render's ~100s gateway). Set to `300` on platforms without a gateway timeout (Fly, Cloud Run) for long thinking generations |
| `HY3_BASE` | `https://tencent-Hy3.hf.space/...` | Upstream Hy3 Gradio endpoint. Override to point at a different Space or self-hosted instance |
| `PRESERVED_THINKING` | `true` | Whether Hy3 includes reasoning text in the response. Set to `false` to disable `reasoning_content` (reduces latency and token counts) |
| `SSE_BUFFER_CAP` | `10000000` | Cap on a single SSE line from upstream in bytes (DoS protection). Default 10MB |
| `MAX_BODY_BYTES` | `8000000` | Reject request bodies larger than this BEFORE parsing (413 Payload Too Large). Default 8MB |
| `TRUST_PROXY_HEADERS` | `true` | Trust `X-Forwarded-For` for client IP. Default true (every platform puts a proxy in front). Set to `false` if exposing directly |
| `LOG_LEVEL` | `INFO` | Python logging level (`DEBUG`/`INFO`/`WARNING`/`ERROR`) |
| `API_KEYS` | _(empty)_ | Comma-separated API keys. If set, `/v1/chat/completions` requires `Authorization: Bearer <key>` |
| `ADMIN_TOKEN` | _(empty)_ | Secret token for `/admin/*` endpoints. If unset, admin endpoints return 404 |
| `ADMIN_ORIGIN` | `*` | Allowed CORS origin. Set to a specific origin (e.g. `https://app.example.com`) to enable credentialed CORS |
| `MAX_MESSAGES` | `1000` | Max messages per request (DoS protection) |
| `MAX_CONTENT_CHARS` | `1000000` | Max total characters across all messages (DoS protection) |

### Models

| Model ID | Behavior |
|---|---|
| `hy3` | Default — no thinking, fast responses |
| `hy3-think` | Auto-enables `think_level=high` (shows reasoning in `reasoning_content`) |

### Hy3-specific parameters

Pass via `extra_body` in the OpenAI SDK or directly in the request JSON:

| Param | Type | Default | Description |
|---|---|---|---|
| `think_level` | `high` \| `low` \| `no_think` | `no_think` | Override thinking level (auto-set to `high` for `hy3-think` model only if not explicitly provided) |

```python
resp = client.chat.completions.create(
    model="hy3",
    messages=[{"role": "user", "content": "Think hard: P vs NP"}],
    extra_body={"think_level": "high"},
)
```

---

## 📊 API Endpoints

| Endpoint | Method | Auth | Description |
|---|---|---|---|
| `/` | GET | — | Service info |
| `/health` | GET | — | Liveness probe — returns 503 when more than 50% of the **last 50** requests errored, and the window holds at least 10 requests. The window slides, so health recovers automatically once bad requests age out; a single upstream blip will not brick the service. Response includes `recent_window` and `recent_error_rate`. |
| `/stats` | GET | — | Runtime counters (active, peak, rejected, completed) |
| `/v1/models` | GET | — | OpenAI models list |
| `/v1/chat/completions` | POST | `API_KEYS` (if set) | OpenAI chat completion (streaming + non-streaming) |
| `/admin/logs` | GET | `ADMIN_TOKEN` | Recent log entries (filterable by level, request_id, event) |
| `/admin/requests` | GET | `ADMIN_TOKEN` | Recent request records (newest first) |
| `/admin/logs/summary` | GET | `ADMIN_TOKEN` | Counts by level + event for dashboard widgets |
| `/admin/requests/{id}` | GET | `ADMIN_TOKEN` | Single request record + all its log entries |

### Stats endpoint

```bash
curl http://localhost:8000/stats
# {
#   "max_concurrent": 10,
#   "queue_timeout_seconds": 5.0,
#   "active_requests": 3,
#   "peak_active": 7,
#   "available_slots": 7,
#   "total_acquired": 142,
#   "total_rejected_503": 0,
#   "total_completed": 139,
#   "total_errors": 1,
#   "uptime_seconds": 3611.4
# }
```

### Admin endpoints

Set `ADMIN_TOKEN` env var, then:

```bash
curl -H "Authorization: Bearer $ADMIN_TOKEN" \
  http://localhost:8000/admin/logs?limit=10

curl -H "Authorization: Bearer $ADMIN_TOKEN" \
  http://localhost:8000/admin/requests?errors_only=true
```

---

## 🛡️ Production Deployment

### Deploy to Render (one-click)

[![Deploy to Render](https://render.com/images/deploy-to-render-button.svg)](https://render.com/deploy)

The included `render.yaml` Blueprint creates a free web service automatically.

### Deploy elsewhere

See **[DEPLOY.md](DEPLOY.md)** for platform-specific setup guides:

| Platform | Free tier | Sleeps? | Request timeout | Notes |
|---|---|---|---|---|
| **Fly.io** | 3 shared-cpu-1x VMs (256MB) | No | ~5min (configurable) | Best free option — global edge |
| **Koyeb** | 1 service (512MB) | No | None | Docker-native, simple CLI |
| **Google Cloud Run** | 2M req/mo + 360k vCPU-seconds | Scales to zero | 60 min (max) | Generous, but cold-start 2-5s |
| **Oracle Cloud Always Free** | 4 ARM cores + 24GB RAM | No | None (self-managed) | Best free compute, self-managed OS |
| **Railway** | $5 trial credit | No (on paid) | None | Closest to Render UX |
| **Hugging Face Spaces** | 2 vCPU / 16GB RAM | Yes (48h idle) | None | Co-located with Hy3 upstream |

### Production security checklist

```bash
# Required for any public deployment:
export API_KEYS="your-secret-key-1,your-secret-key-2"
export ADMIN_TOKEN="your-admin-secret"

# For browser-based frontends:
export ADMIN_ORIGIN="https://your-frontend.example.com"

# Tune for your workload:
export MAX_CONCURRENT=20
export QUEUE_TIMEOUT=10
export LOG_LEVEL="INFO"
```

---

## 🧰 CLI Client (`hy3.sh`)

```bash
Usage: hy3.sh [OPTIONS] "prompt"

Options:
  -s TEXT   System prompt
  -t LVL    Think level: high, low, no_think (default: no_think)
  -m NUM    Max tokens (default: 262144)
  -T FLOAT  Temperature
  -p FLOAT  Top-p
  -c ID     Conversation ID for multi-turn (saves/loads history to ~/.hy3_state/)
            ID must be alphanumeric (a-z, 0-9, -, _) — sanitized to prevent path traversal
  -f FILE   Tools/functions JSON file (or inline JSON string)
  -r        Raw output (no streaming, just final text)
  -h        Help

Examples:
  hy3.sh "explain RSA in 3 sentences"
  hy3.sh -t high "prove the halting problem is undecidable"
  hy3.sh -s "You are a pirate" "tell me about cryptography"
  hy3.sh -c mychat "hello" && hy3.sh -c mychat "what did I just say?"
  hy3.sh -f tools.json "What is the weather in Tokyo?"
```

### Tool calling with the CLI

When the model calls a tool, the script outputs a JSON envelope to stderr:

```
TOOL_CALL:{"id": "call_abc123", "name": "get_weather", "arguments": "{\"location\": \"Tokyo\"}"}
```

Feed tool results back as a follow-up message:

```bash
./hy3.sh -c mychat "Tool result: {\"temp\": \"28°C\"}. Answer the question."
```

---

## 🏗️ How It Works

Calls the HuggingFace Gradio API endpoint for the `tencent/Hy3` space:

```
POST https://tencent-Hy3.hf.space/gradio_api/call/chat
GET  https://tencent-Hy3.hf.space/gradio_api/call/chat/{event_id}
```

No API key. No auth. HF routes through their inference provider network (deepinfra backend).

### Architecture

```
┌─────────────────┐       ┌──────────────────────────┐       ┌─────────────────────┐
│  OpenAI client  │──────▶│  hy3-openai-api (FastAPI)│──────▶│  HuggingFace Gradio │
│  / SDK / curl   │◀──────│  • concurrency limiter   │◀──────│  tencent/Hy3 (295B) │
└─────────────────┘  SSE  │  • input validation      │  SSE  └─────────────────────┘
                         │  • CORS / API key auth    │
                         │  • structured logging     │
                         │  • in-memory ring buffer  │
                         └──────────────────────────┘
```

---

## 🧪 Development

### Run the server with auto-reload

```bash
python server.py --port 8000 --reload
```

### Project structure

```
hy3-openai-api/
├── server.py              # FastAPI server — OpenAI-compatible API
├── logging_layer.py       # Structured logging + in-memory ring buffer
├── hy3.sh                 # CLI client (bash + python)
├── tools_example.json     # Sample tool-calling config
├── Dockerfile             # Docker image (python:3.11.9-slim-bookworm)
├── render.yaml            # Render Blueprint for one-click deploy
├── requirements.txt       # Pinned Python dependencies
├── examples/              # Tool config snippets (Kilocode, Opencode)
│   ├── kilo.json.example
│   └── opencode.json.example
├── DEPLOY.md              # Platform-specific deployment guides
├── TOOLS.md               # AI tool compatibility guide (Kilocode, Opencode, etc.)
├── CHANGELOG.md           # Version history
├── LICENSE                # MIT
└── README.md              # This file
```

---

## 📝 License

MIT — see [LICENSE](LICENSE).

---

## 🙏 Acknowledgments

- [Tencent Hy3](https://huggingface.co/spaces/tencent/Hy3) — the 295B MoE model
- [HuggingFace](https://huggingface.co/) — for hosting the Gradio API
- [FastAPI](https://fastapi.tiangolo.com/) — the web framework
- [httpx](https://www.python-httpx.org/) — async HTTP client
