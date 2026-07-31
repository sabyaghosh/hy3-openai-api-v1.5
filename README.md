# Hy3 Client

Free CLI client for [Tencent Hy3](https://huggingface.co/spaces/tencent/Hy3) — 295B MoE model via HuggingFace's Gradio API. No API key required.

## Features

- **Streaming** — tokens appear as they're generated
- **Thinking mode** — see the model's reasoning process
- **Multi-turn** — conversation history persists across calls
- **Tool calling** — define functions, model calls them
- **Preserved thinking** — always enabled (262,144 max tokens)
- **OpenAI-compatible API server** — drop-in replacement for OpenAI base_url

## Install

```bash
curl -sL https://raw.githubusercontent.com/YOUR_USER/hy3-client/main/hy3.sh -o hy3.sh
chmod +x hy3.sh
```

## Usage

```bash
# Basic
./hy3.sh "explain RSA in 3 sentences"

# Thinking mode (shows reasoning)
./hy3.sh -t high "prove the halting problem is undecidable"

# Multi-turn conversation
./hy3.sh -c mychat "my name is LO"
./hy3.sh -c mychat "what's my name?"

# Tool calling
./hy3.sh -f tools.json "What is the weather in Tokyo?"

# System prompt
./hy3.sh -s "You are a pirate" "tell me about cryptography"

# Pipe input
echo "summarize this" | ./hy3.sh

# Raw output (no streaming)
./hy3.sh -r "hello"
```

## Options

| Flag | Description | Default |
|------|-------------|---------|
| `-s TEXT` | System prompt | — |
| `-t LVL` | Think level: `high`, `low`, `no_think` | `no_think` |
| `-m NUM` | Max tokens | `262144` |
| `-T FLOAT` | Temperature | model default |
| `-p FLOAT` | Top-p | model default |
| `-c ID` | Conversation ID (multi-turn with persistence) | — |
| `-f FILE` | Tools/functions JSON file | — |
| `-r` | Raw output (no streaming) | off |

## Tool Calling

Create a tools JSON file:

```json
[
  {
    "type": "function",
    "function": {
      "name": "get_weather",
      "description": "Get weather info for a city",
      "parameters": {
        "type": "object",
        "properties": {
          "location": {"type": "string", "description": "City name"}
        },
        "required": ["location"]
      }
    }
  }
]
```

Then:
```bash
./hy3.sh -f tools.json "What is the weather in Tokyo?"
# Output: TOOL_CALL:id:get_weather:{"location": "Tokyo"}
```

Feed results back:
```bash
./hy3.sh -c chat1 "Tool result: {\"temp\": \"28°C\"}. Answer the question."
```

## How It Works

Calls the HuggingFace Gradio API endpoint for the `tencent/Hy3` space:

```
POST https://tencent-Hy3.hf.space/gradio_api/call/chat
GET  https://tencent-Hy3.hf.space/gradio_api/call/chat/{event_id}
```

No API key. No auth. HF routes through their inference provider network (deepinfra backend).

## OpenAI-Compatible API Server

`server.py` exposes Hy3 as an OpenAI-compatible API. Drop-in replacement for `https://api.openai.com/v1`.

### Run

```bash
pip install -r requirements.txt
python server.py --port 8000
```

Server listens on `http://localhost:8000`. No API key needed — pass any string.

### Deploy to Render

This repo includes a `Dockerfile` and `render.yaml` (Blueprint). One-click deploy:

[![Deploy to Render](https://render.com/images/deploy-to-render-button.svg)](https://render.com/deploy)

Or manually:

1. Push this repo to GitHub.
2. On Render: **New → Blueprint** → select the repo.
3. Render reads `render.yaml` and creates a free web service.
4. Once deployed, point your OpenAI client at `https://<your-service>.onrender.com/v1`.

#### Run with Docker locally

```bash
docker build -t hy3-openai-api .
docker run -p 8000:8000 hy3-openai-api
# API now at http://localhost:8000/v1
```

#### Environment variables

| Var | Default | Description |
|---|---|---|
| `HOST` | `0.0.0.0` | Bind host |
| `PORT` | `8000` | Bind port (Render injects this automatically) |
| `PYTHONUNBUFFERED` | — | Set to `1` for unbuffered logs |

### Models

| Model ID | Behavior |
|---|---|
| `hy3` | Default — no thinking, fast responses |
| `hy3-think` | Auto-enables `think_level=high` (shows reasoning in `reasoning_content`) |

### Endpoints

| Endpoint | Method | Description |
|---|---|---|
| `GET /v1/models` | GET | List available models |
| `POST /v1/chat/completions` | POST | Chat completion (streaming + non-streaming) |
| `GET /` | GET | Service info |

### Use with the OpenAI Python SDK

```bash
pip install openai
```

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

# Streaming with thinking mode
stream = client.chat.completions.create(
    model="hy3-think",
    messages=[{"role": "user", "content": "Prove the halting problem is undecidable."}],
    stream=True,
)
for chunk in stream:
    delta = chunk.choices[0].delta
    if getattr(delta, "reasoning_content", None):
        print(delta.reasoning_content, end="", flush=True)
    if delta.content:
        print(delta.content, end="", flush=True)

# Tool calling
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

### Use with curl

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

### Use with LangChain / LlamaIndex / anything else

Point any OpenAI-compatible client at `http://localhost:8000/v1` with any API key string.

### Hy3-specific parameters

Pass via `extra_body` in the OpenAI SDK or directly in the request JSON:

| Param | Type | Default | Description |
|---|---|---|---|
| `think_level` | `high` \| `low` \| `no_think` | `no_think` | Override thinking level (auto-set to `high` for `hy3-think` model) |

```python
resp = client.chat.completions.create(
    model="hy3",
    messages=[{"role": "user", "content": "Think hard: P vs NP"}],
    extra_body={"think_level": "high"},
)
```

## License

MIT
