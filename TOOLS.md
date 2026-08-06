# AI Tool Compatibility Guide

This document walks through how to configure popular AI coding agents to use the Hy3 OpenAI-compatible API as their LLM backend. Hy3 v1.5.0+ is fully compatible with both **Kilocode** (kilo.ai) and **Opencode** (opencode.ai) — two of the most popular open-source AI coding agents.

For background on why v1.5.0 was needed and what changed, see [CHANGELOG.md](CHANGELOG.md). For deployment instructions, see [DEPLOY.md](DEPLOY.md).

---

## Quick start

1. Start the Hy3 server locally:
   ```bash
   pip install -r requirements.txt
   python server.py --port 8000
   ```
2. Copy the relevant example config from `examples/` to your project root.
3. Set the `HY3_API_KEY` environment variable (any non-empty string for local dev).
4. Run your tool from the project directory.

---

## Kilocode (kilo.ai)

[Kilocode](https://kilo.ai) is an open-source AI coding agent available as a VS Code extension, JetBrains extension, and CLI. It uses the [Vercel AI SDK](https://ai-sdk.dev)'s `@ai-sdk/openai-compatible` package as its HTTP client for custom OpenAI-compatible providers.

### Setup

1. Install the Kilo CLI:
   ```bash
   curl -fsSL https://kilo.ai/cli/install | bash
   ```
2. Copy [`examples/kilo.json.example`](examples/kilo.json.example) to your project root as `kilo.json`.
3. Set your API key:
   ```bash
   export HY3_API_KEY=anything   # local dev: Hy3 ignores the value
   ```
4. From your project directory, run:
   ```bash
   kilo
   ```
   Or non-interactively:
   ```bash
   kilo run --model hy3local/hy3 "Say hello in 3 words."
   ```

### Configuration reference

| Field | Purpose |
|---|---|
| `provider.hy3local.options.baseURL` | The Hy3 server URL (with `/v1` suffix) |
| `provider.hy3local.options.apiKey` | API key. Use `{env:HY3_API_KEY}` to read from env. Local dev: any string works. |
| `provider.hy3local.options.includeUsage` | **Required for v1.5.0 streaming usage support.** Set to `true`. |
| `provider.hy3local.models.<id>.limit.context` | Context window size in tokens. Used for auto-compaction. |
| `provider.hy3local.models.<id>.limit.output` | Max output tokens per response. |
| `provider.hy3local.models.<id>.tool_call` | Whether the model supports tool/function calling. |
| `provider.hy3local.models.<id>.reasoning` | Whether the model exposes reasoning content (only `hy3-think`). |

### Using the hy3-think model (reasoning mode)

Switch the model in your `kilo.json` to `hy3local/hy3-think` to enable reasoning mode. The model's chain-of-thought will be exposed via the `reasoning_content` field and surfaced in Kilocode's TUI thinking panel.

### Verification checklist

After setup, verify:
- [ ] `kilo models` lists `hy3local/hy3` and `hy3local/hy3-think`
- [ ] `kilo run --model hy3local/hy3 "Say hello in 3 words."` returns a non-empty response
- [ ] `kilo stats` shows non-zero token usage after at least one request
- [ ] Multi-turn conversation retains context
- [ ] Tool calling works (e.g. ask "what's the weather in Paris?" with a `get_weather` tool)

### Troubleshooting

**"Model not found"** — Verify your `kilo.json` is in the project root and the `model` field matches the format `hy3local/hy3` (provider_id/model_id).

**"Context usage count is always 0"** — You're missing `includeUsage: true` in the provider options. Without it, the Vercel AI SDK does not request streaming usage and token tracking silently fails.

**"Tool call returns null arguments"** — This was a v1.4.5 bug fixed in v1.5.0 by WP3. Verify your server is v1.5.0 or later by checking `GET /` returns `"version": "1.5.0"`.

---

## Opencode (opencode.ai)

[Opencode](https://opencode.ai) is an open-source AI coding agent from Anomaly. Like Kilocode, it uses the Vercel AI SDK's `@ai-sdk/openai-compatible` package for OpenAI-compatible providers.

### Setup

1. Install the Opencode CLI:
   ```bash
   curl -fsSL https://opencode.ai/install | bash
   ```
2. Copy [`examples/opencode.json.example`](examples/opencode.json.example) to your project root as `opencode.json`.
3. Set your API key:
   ```bash
   export HY3_API_KEY=anything   # local dev: Hy3 ignores the value
   ```
4. From your project directory, run:
   ```bash
   opencode
   ```
   Or non-interactively:
   ```bash
   opencode run --model hy3local/hy3 "Say hello in 3 words."
   ```

### Configuration reference

| Field | Purpose |
|---|---|
| `provider.hy3local.npm` | Must be `@ai-sdk/openai-compatible` (uses Chat Completions API) |
| `provider.hy3local.options.baseURL` | The Hy3 server URL (with `/v1` suffix) |
| `provider.hy3local.options.apiKey` | API key. Use `{env:HY3_API_KEY}` to read from env. |
| `provider.hy3local.options.includeUsage` | **Required for v1.5.0 streaming usage support.** Set to `true`. |
| `provider.hy3local.models.<id>.limit.context` | Context window size in tokens. |
| `provider.hy3local.models.<id>.limit.output` | Max output tokens per response. |

### Using the hy3-think model (reasoning mode)

Switch the model by passing `--model hy3local/hy3-think` to `opencode run`, or by setting `"model": "hy3local/hy3-think"` in your `opencode.json`.

### Verification checklist

After setup, verify:
- [ ] `opencode models` lists `hy3local/hy3` and `hy3local/hy3-think`
- [ ] `opencode run --model hy3local/hy3 "Say hello in 3 words."` returns a non-empty response
- [ ] `opencode stats` shows non-zero token usage after at least one request
- [ ] Multi-turn conversation retains context
- [ ] Tool calling works (e.g. ask "what's the weather in Paris?" with a `get_weather` tool)
- [ ] Reasoning visible in TUI thinking panel when using `hy3local/hy3-think`

### Troubleshooting

**"Context usage count is always 0"** — Same root cause as Kilocode. Add `includeUsage: true` to the provider options. See upstream issue [anomalyco/opencode#423](https://github.com/anomalyco/opencode/issues/423) for background.

**"No tool call received"** — Verify the server is v1.5.0+ (was a v1.4.5 bug fixed by WP3). Check `GET /` returns `"version": "1.5.0"`.

---

## Production deployment

For production deployments, replace the `baseURL` value with your deployed Hy3 instance URL (e.g. `https://hy3.your-domain.com/v1`), set `API_KEYS` on the server to a strong random secret (see [`.env.example`](.env.example) for the recommended generation command), and set `HY3_API_KEY` in the user's environment to the same value. The rest of the config is identical to the local development snippet.

See [DEPLOY.md](DEPLOY.md) for platform-specific deployment guides (Render, Fly.io, Cloud Run, Koyeb, Railway, Hugging Face Spaces, Oracle Cloud).

---

## How Hy3 v1.5.0 achieves compatibility

Both Kilocode and Opencode use the Vercel AI SDK's [`@ai-sdk/openai-compatible`](https://ai-sdk.dev/providers/openai-compatible-providers) package as their HTTP client. This package enforces a stricter subset of the OpenAI specification than the official Python SDK. The v1.5.0 release closes the three critical gaps that prevented compatibility:

1. **Streaming usage support (WP1)** — When `stream_options.include_usage` is true, Hy3 now emits a final SSE chunk with the populated `usage` object before `[DONE]`. Required by the AI SDK's `includeUsage` provider setting.

2. **Model metadata enrichment (WP2)** — `/v1/models` now returns `context_length`, `max_tokens`, `tool_call`, `reasoning`, `supports_parallel_tool_calls`, and `supports_structured_outputs` fields per model. Enables auto-compaction in both tools without manual `limit.context`/`limit.output` configuration.

3. **Tool-call streaming delta conformance (WP3)** — Tool-call deltas are now emitted incrementally across the SSE stream, each with the `index` field identifying which tool call the delta applies to. The first delta for an index carries `id` + `function.name`; subsequent deltas carry `function.arguments` fragments.

Plus several secondary OpenAI-spec coverage improvements (WP4-WP6). See [CHANGELOG.md](CHANGELOG.md) for the complete list.

---

## Verification protocol (for maintainers)

Before each release, re-run this protocol to verify compatibility:

1. Start the Hy3 server locally: `python server.py --port 8000`
2. Configure Kilocode with `examples/kilo.json.example` (set `baseURL` to `http://127.0.0.1:8000/v1`).
3. Run `kilo run --model hy3local/hy3 "Say hello in 3 words"`. Verify a non-empty response.
4. Run `kilo stats`. Verify non-zero token usage.
5. Repeat with a tool-calling prompt. Verify the tool call surfaces with correct name + arguments.
6. Switch model to `hy3local/hy3-think`. Verify reasoning is visible in the TUI.
7. Repeat steps 2-6 with Opencode.

If any step fails, debug via the logging proxy (`scripts/logging_proxy.py` from the research phase) to capture the exact HTTP traffic between the tool and the Hy3 server.
