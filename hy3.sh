#!/usr/bin/env bash
# hy3.sh — Tencent Hy3 via HuggingFace Gradio API (free, no key)
# Usage:
#   ./hy3.sh "your prompt here"
#   ./hy3.sh -s "system prompt" "your prompt here"
#   ./hy3.sh -t high "your prompt here"          # think level: high/low/no_think
#   ./hy3.sh -m 2000 "your prompt here"           # max tokens
#   ./hy3.sh -f tools.json "call a tool"          # tool calling
#   echo "prompt" | ./hy3.sh                       # stdin
#   ./hy3.sh -c "conversation id" "follow up"     # multi-turn (saves to .hy3_state/)

set -euo pipefail

BASE="https://tencent-Hy3.hf.space/gradio_api/call/chat"
STATE_DIR="${HOME}/.hy3_state"
SYSTEM_PROMPT=""
THINK_LEVEL="no_think"
MAX_TOKENS=262144
PRESERVED_THINKING="true"
TEMPERATURE=""
TOP_P=""
CONV_ID=""
RAW=0
TOOLS_JSON=""

# Verify python3 is available before we depend on it later
command -v python3 >/dev/null 2>&1 || {
  echo "Error: python3 is required but not installed" >&2
  exit 1
}

usage() {
  cat <<EOF
Usage: $(basename "$0") [OPTIONS] "prompt"

Options:
  -s TEXT   System prompt
  -t LVL    Think level: high, low, no_think (default: no_think)
  -m NUM    Max tokens (default: 262144)
  -T FLOAT  Temperature
  -p FLOAT  Top-p
  -c ID     Conversation ID for multi-turn (saves/loads history)
  -f FILE   Tools/functions JSON file (or inline JSON string)
  -r        Raw output (no streaming, just final text)
  -h        Help

Tool Calling:
  Define tools with -f. When the model calls a tool, the script outputs a
  JSON envelope to stderr:
    TOOL_CALL:{"id": "call_abc123", "name": "get_weather", "arguments": "{\"location\": \"Tokyo\"}"}
  Feed tool results back with -c and a tool result message.

Examples:
  $(basename "$0") "explain RSA in 3 sentences"
  $(basename "$0") -t high "prove the halting problem is undecidable"
  $(basename "$0") -s "You are a pirate" "tell me about cryptography"
  $(basename "$0") -c mychat "hello" && $(basename "$0") -c mychat "what did I just say?"
  $(basename "$0") -f tools.json "What is the weather in Tokyo?"
EOF
  exit 0
}

while getopts "s:t:m:T:p:c:f:rh" opt; do
  case $opt in
    s) SYSTEM_PROMPT="$OPTARG" ;;
    t) THINK_LEVEL="$OPTARG" ;;
    m) MAX_TOKENS="$OPTARG" ;;
    T) TEMPERATURE="$OPTARG" ;;
    p) TOP_P="$OPTARG" ;;
    c) CONV_ID="$OPTARG" ;;
    f) TOOLS_JSON="$OPTARG" ;;
    r) RAW=1 ;;
    h) usage ;;
    *) usage ;;
  esac
done
shift $((OPTIND - 1))

# Get prompt from arg or stdin
if [[ $# -gt 0 ]]; then
  PROMPT="$*"
elif [[ ! -t 0 ]]; then
  PROMPT="$(cat)"
else
  echo "Error: no prompt provided" >&2
  usage
fi

if [[ -z "$PROMPT" ]]; then
  echo "Error: empty prompt" >&2
  exit 1
fi

# Load conversation history if -c specified
HISTORY="null"
if [[ -n "$CONV_ID" ]]; then
  # Sanitize CONV_ID to prevent path traversal (e.g. "../../etc/passwd")
  if [[ ! "$CONV_ID" =~ ^[a-zA-Z0-9_-]+$ ]]; then
    echo "Error: conversation ID must be alphanumeric (a-z, 0-9, -, _)" >&2
    exit 1
  fi
  mkdir -p "$STATE_DIR"
  STATE_FILE="$STATE_DIR/${CONV_ID}.json"
  if [[ -f "$STATE_FILE" ]]; then
    HISTORY="$(cat "$STATE_FILE")"
  fi
fi

# Load tools JSON
TOOLS_DATA="[]"
if [[ -n "$TOOLS_JSON" ]]; then
  if [[ -f "$TOOLS_JSON" ]]; then
    TOOLS_DATA="$(cat "$TOOLS_JSON")"
  else
    TOOLS_DATA="$TOOLS_JSON"
  fi
fi

# Build data array
build_payload() {
  local temp_val="null"
  local top_val="null"
  [[ -n "$TEMPERATURE" ]] && temp_val="$TEMPERATURE"
  [[ -n "$TOP_P" ]] && top_val="$TOP_P"

  python3 -c "
import json, sys
msg = sys.argv[1]
sp = sys.argv[2]
hist = json.loads(sys.argv[3])
tl = sys.argv[4]
mt = int(sys.argv[5])
temp = json.loads(sys.argv[6])
tp = json.loads(sys.argv[7])
pt = sys.argv[8] == 'true'
tools = json.loads(sys.argv[9])
print(json.dumps({'data': [msg, sp, hist, tl, temp, mt, tp, pt, json.dumps(tools)]}))
" "$PROMPT" "$SYSTEM_PROMPT" "$HISTORY" "$THINK_LEVEL" "$MAX_TOKENS" "$temp_val" "$top_val" "$PRESERVED_THINKING" "$TOOLS_DATA"
}

PAYLOAD="$(build_payload)"

# Step 1: POST to get event_id
# add --max-time to prevent hanging forever if upstream is unresponsive.
RESPONSE=$(curl -sf --max-time 30 -X POST "$BASE" \
  -H "Content-Type: application/json" \
  -d "$PAYLOAD")

EVENT_ID=$(echo "$RESPONSE" | python3 -c "import sys,json; print(json.load(sys.stdin)['event_id'])")

if [[ -z "$EVENT_ID" ]]; then
  echo "Error: failed to get event_id" >&2
  exit 1
fi

# Step 2: Stream and save everything to a temp file, then replay
# add --max-time to prevent hanging forever on a stalled stream.
TMPFILE=$(mktemp)
ERRFILE=$(mktemp)
trap "rm -f '$TMPFILE' '$ERRFILE'" EXIT

# Don't swallow curl errors. Capture exit code + check for empty output.
# redirect stderr to a separate file (was 2>&1 which mixed errors into the
# SSE data, breaking the JSON parser). Now errors go to ERRFILE for diagnostics.
if ! curl -sf --max-time 300 -N "${BASE}/${EVENT_ID}" > "$TMPFILE" 2>"$ERRFILE"; then
  echo "Error: failed to fetch Hy3 stream (curl exit $?)" >&2
  if [[ -s "$ERRFILE" ]]; then
    echo "--- curl stderr ---" >&2
    head -c 500 "$ERRFILE" >&2
  fi
  if [[ -s "$TMPFILE" ]]; then
    echo "--- Response body ---" >&2
    head -c 500 "$TMPFILE" >&2
  fi
  exit 1
fi
if [[ ! -s "$TMPFILE" ]]; then
  echo "Error: empty response from Hy3" >&2
  exit 1
fi

# Output: stream or raw
export HY3_MODE
HY3_MODE="$( [[ "$RAW" -eq 1 ]] && echo raw || echo stream )"
export HY3_CONV="${CONV_ID:-}"
export HY3_TMP="$TMPFILE"
export HY3_STATE="$STATE_DIR"
python3 << 'PYEOF'
import json, sys, os

mode = os.environ.get('HY3_MODE', 'stream')
conv_id = os.environ.get('HY3_CONV', '')
tmpfile = os.environ.get('HY3_TMP', '')
state_dir = os.environ.get('HY3_STATE', '')

# Use `with` to avoid file handle leak
with open(tmpfile, encoding='utf-8') as f:
    lines = f.read().split('\n')

final_data = None
last_think = 0
last_resp = 0
think_started = False

for line in lines:
    if line.startswith('data: '):
        payload = line[6:]
        try:
            data = json.loads(payload)
            # Verify the payload has the expected Hy3 shape: [[resp, think, tools, ...]]
            # Accessing data[0][0] raises IndexError/TypeError if malformed, which
            # the except block catches to skip this SSE line.
            if not isinstance(data, list) or not data or not isinstance(data[0], list):
                continue
        except Exception:  # was bare `except:` which catches KeyboardInterrupt
            continue

        resp_text = data[0][0] or ''
        think_text = data[0][1] if len(data[0]) > 1 and data[0][1] else ''
        tool_calls = data[0][2] if len(data[0]) > 2 and data[0][2] else []

        if mode == 'stream':
            if think_text and len(think_text) > last_think:
                if not think_started:
                    sys.stderr.write('[thinking]\n')
                    sys.stderr.flush()
                    think_started = True
                sys.stderr.write(think_text[last_think:])
                sys.stderr.flush()
                last_think = len(think_text)
            if resp_text and len(resp_text) > last_resp:
                if think_started and last_resp == 0:
                    sys.stderr.write('\n[response]\n')
                    sys.stderr.flush()
                sys.stdout.write(resp_text[last_resp:])
                sys.stdout.flush()
                last_resp = len(resp_text)

        final_data = data

# Output tool calls if present
if final_data and len(final_data[0]) > 2 and final_data[0][2]:
    tool_calls = final_data[0][2]
    if tool_calls:
        for tc in tool_calls:
            fn = tc.get('function', {})
            name = fn.get('name', 'unknown')
            args = fn.get('arguments', '{}')
            tc_id = tc.get('id', '')
            # Use JSON envelope so colons in args don't break parsing.
            # Old format: TOOL_CALL:id:name:args  (ambiguous if args contains ':')
            # New format: TOOL_CALL:{json}
            envelope = json.dumps({
                'id': tc_id,
                'name': name,
                'arguments': args if isinstance(args, str) else json.dumps(args),
            }, ensure_ascii=False)
            print(f'\nTOOL_CALL:{envelope}', file=sys.stderr)

if mode == 'raw' and final_data:
    text = final_data[0][0] or ''
    if not text and len(final_data[0]) > 1 and final_data[0][1]:
        text = final_data[0][1]
    print(text)
elif mode == 'stream':
    if last_resp == 0 and last_think > 0:
        sys.stderr.write('\n')
    print()

if conv_id and final_data and state_dir:
    try:
        os.makedirs(state_dir, exist_ok=True)
        messages = final_data[0][3]
        with open(os.path.join(state_dir, conv_id + '.json'), 'w', encoding='utf-8') as f:
            json.dump(messages, f, ensure_ascii=False)
    except Exception as e:  # was `pass` — silent failure
        sys.stderr.write(f'Warning: failed to save conversation state: {e}\n')
PYEOF
