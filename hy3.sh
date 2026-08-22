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

# v1.5.1 (#40): split help from failure paths.
# - `print_usage` writes the help text to stdout and returns 0 (for -h).
# - `usage_error MSG` writes the error + help to stderr and exits 1 (for
#   invalid options, missing prompt, etc.).
# Previously, usage() always exit 0, which meant invalid invocation was
# indistinguishable from success in automation.
print_usage() {
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
}

usage_error() {
  # Print error message + usage to stderr, exit 1.
  echo "Error: $1" >&2
  echo "" >&2
  print_usage >&2
  exit 1
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
    # v1.5.1 (#40): -h exits 0 (help), unknown opt exits 1 (error).
    h) print_usage; exit 0 ;;
    *) usage_error "unknown option: -$opt" ;;
  esac
done
shift $((OPTIND - 1))

# Validate -t (think level) and -m (max tokens) early so users get a clear
# error instead of a Python traceback from build_payload.
if [[ -n "$THINK_LEVEL" ]]; then
  case "$THINK_LEVEL" in
    high|low|no_think) ;;
    *) echo "Error: -t must be one of: high, low, no_think (got: $THINK_LEVEL)" >&2; exit 1 ;;
  esac
fi
if ! [[ "$MAX_TOKENS" =~ ^[0-9]+$ ]] || [[ "$MAX_TOKENS" -lt 1 ]]; then
  echo "Error: -m must be a positive integer (got: $MAX_TOKENS)" >&2
  exit 1
fi

# Validate -T / -p too. They are interpolated into build_payload's `json.loads`
# arguments, so a non-numeric value produced a raw Python traceback rather than
# a usage error.
_is_number() {
  [[ "$1" =~ ^-?([0-9]+(\.[0-9]*)?|\.[0-9]+)$ ]]
}
if [[ -n "$TEMPERATURE" ]] && ! _is_number "$TEMPERATURE"; then
  echo "Error: -T must be a number (got: $TEMPERATURE)" >&2
  exit 1
fi
if [[ -n "$TOP_P" ]] && ! _is_number "$TOP_P"; then
  echo "Error: -p must be a number (got: $TOP_P)" >&2
  exit 1
fi

# Get prompt from arg or stdin
if [[ $# -gt 0 ]]; then
  PROMPT="$*"
elif [[ ! -t 0 ]]; then
  PROMPT="$(cat)"
else
  # v1.5.1 (#40): missing prompt is an error, not a help request.
  usage_error "no prompt provided"
fi

if [[ -z "$PROMPT" ]]; then
  # v1.5.1 (#40): empty prompt is an error.
  usage_error "empty prompt"
fi

# Load conversation history if -c specified.
# v1.5.1 (#37): validate the state file is valid JSON before using it.
# A partially written or manually damaged state file used to terminate the
# CLI with a raw Python traceback from build_payload's json.loads.
# Default is "[]" (empty list) to match the server's wire format — the server
# always sends a list in this slot, so the CLI should too.
HISTORY="[]"
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
    # #37: validate history parses as JSON before we hand it to build_payload.
    if ! echo "$HISTORY" | python3 -c "import sys,json; json.load(sys.stdin)" 2>/dev/null; then
      echo "Error: conversation state file is corrupt (not valid JSON): $STATE_FILE" >&2
      echo "  Delete the file to start a fresh conversation: rm $STATE_FILE" >&2
      exit 1
    fi
  fi
fi

# Load tools JSON — validate that it's either a readable file or valid JSON.
# v1.5.1 (#36): file-based tools are now validated with json.loads before
# being accepted. Previously, a readable but invalid JSON file was accepted
# and then crashed build_payload with a traceback.
TOOLS_DATA="[]"
if [[ -n "$TOOLS_JSON" ]]; then
  if [[ -f "$TOOLS_JSON" ]]; then
    TOOLS_DATA="$(cat "$TOOLS_JSON")"
    # #36: validate file contents parse as JSON.
    if ! echo "$TOOLS_DATA" | python3 -c "import sys,json; json.load(sys.stdin)" 2>/dev/null; then
      echo "Error: -f file is readable but not valid JSON: $TOOLS_JSON" >&2
      exit 1
    fi
  else
    # Not a file — treat as inline JSON. Validate it parses.
    if ! echo "$TOOLS_JSON" | python3 -c "import sys,json; json.load(sys.stdin)" 2>/dev/null; then
      echo "Error: -f argument is neither a readable file nor valid JSON: $TOOLS_JSON" >&2
      exit 1
    fi
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

# Step 1: POST to get event_id.
# Use `|| rc=$?` idiom because `set -e` terminates on assignment failure
# before `rc=$?` can execute. The `||` form is set -e-safe.
curl_rc=0
RESPONSE=$(curl -sf --max-time 30 -X POST "$BASE" \
  -H "Content-Type: application/json" \
  -d "$PAYLOAD" 2>/dev/null) || curl_rc=$?
if [[ $curl_rc -ne 0 ]]; then
  echo "Error: Hy3 POST failed (curl exit $curl_rc)" >&2
  echo "  (28=timeout, 22=HTTP error, 6=DNS, 7=connection refused)" >&2
  exit 1
fi

# Extract event_id, with error handling for unexpected JSON.
# Note: capture stdout only; let stderr go to the console for diagnostics.
# Use the `|| rc=$?` idiom: under `set -e` a failing command substitution in an
# assignment terminates the shell immediately, so a following `if [[ $? -ne 0 ]]`
# is unreachable dead code. `set -o pipefail` makes the pipeline report python3's
# exit status.
parse_rc=0
EVENT_ID=$(echo "$RESPONSE" | python3 -c "
import sys, json
try:
    d = json.load(sys.stdin)
    print(d.get('event_id', ''))
except Exception as e:
    sys.stderr.write(f'Error: failed to parse event_id from response: {e}\n')
    sys.exit(1)
") || parse_rc=$?
if [[ $parse_rc -ne 0 ]]; then
  echo "Error: failed to parse event_id from Hy3 response" >&2
  echo "  Response: ${RESPONSE:0:200}" >&2
  exit 1
fi

if [[ -z "$EVENT_ID" ]]; then
  echo "Error: Hy3 returned no event_id" >&2
  echo "  Response: ${RESPONSE:0:200}" >&2
  exit 1
fi

# Step 2: Stream and save everything to a temp file, then replay
# --max-time prevents hanging forever on a stalled stream.
#
# Pre-initialise BOTH vars to "" and install the trap before either mktemp, so
# that a failure in either one still runs a well-defined cleanup. Previously the
# trap was installed between the two mktemp calls while referencing $ERRFILE; if
# the second mktemp failed, `set -u` made the trap itself abort with
# "ERRFILE: unbound variable", hiding the real error. `${VAR:-}` inside the
# handler is belt-and-braces against the same class of bug.
TMPFILE=""
ERRFILE=""
_cleanup() {
  [[ -n "${TMPFILE:-}" ]] && rm -f "$TMPFILE"
  [[ -n "${ERRFILE:-}" ]] && rm -f "$ERRFILE"
  return 0  # never let cleanup change the script's exit status
}
trap _cleanup EXIT INT TERM
TMPFILE=$(mktemp)
ERRFILE=$(mktemp)

# Capture curl exit code using `|| rc=$?` idiom (set -e-safe).
curl_rc=0
curl -sf --max-time 300 -N "${BASE}/${EVENT_ID}" > "$TMPFILE" 2>"$ERRFILE" || curl_rc=$?
if [[ $curl_rc -ne 0 ]]; then
  echo "Error: failed to fetch Hy3 stream (curl exit $curl_rc)" >&2
  echo "  (28=timeout, 22=HTTP error, 6=DNS, 7=connection refused)" >&2
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
            # v1.5.1 (#38): the previous check only verified data[0] is a list,
            # NOT that it contains an element. A payload shaped as [[]] would
            # pass the check, then crash on data[0][0] below with IndexError
            # — which is OUTSIDE the try/except block. Now we verify
            # len(data[0]) >= 1 and handle the case where data[0][0] is None
            # or missing.
            if not isinstance(data, list) or not data or not isinstance(data[0], list):
                continue
            if len(data[0]) == 0:
                # Empty inner list — no response text. Skip this SSE line.
                continue
        except Exception:  # was bare `except:` which catches KeyboardInterrupt
            continue

        # #38: data[0][0] may be None or a non-string; coerce safely.
        raw_resp = data[0][0]
        resp_text = raw_resp if isinstance(raw_resp, str) else (str(raw_resp) if raw_resp is not None else '')
        raw_think = data[0][1] if len(data[0]) > 1 else None
        think_text = raw_think if isinstance(raw_think, str) else (str(raw_think) if raw_think is not None else '')
        # tool_calls are handled after the loop from final_data — not here.

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
# v1.5.1 (#39): verify each tc is a dict before calling .get() on it.
# Malformed upstream tool-call data (e.g. a bare string or list) used to
# crash the CLI with AttributeError after an otherwise successful generation.
if final_data and len(final_data[0]) > 2 and final_data[0][2]:
    tool_calls = final_data[0][2]
    if isinstance(tool_calls, list):
        for tc in tool_calls:
            if not isinstance(tc, dict):
                sys.stderr.write(f'Warning: skipping malformed tool call (not a dict): {tc!r}\n')
                continue
            fn = tc.get('function', {})
            if not isinstance(fn, dict):
                fn = {}
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

# v1.5.1 (#41): raw mode must report when no valid completion was received,
# instead of silently succeeding with empty output.
if mode == 'raw':
    if not final_data:
        sys.stderr.write('Error: no valid completion received from upstream\n')
        sys.exit(1)
    # #38: final_data[0][0] may be None or non-string; coerce safely.
    raw_text = final_data[0][0] if (isinstance(final_data[0], list) and final_data[0]) else None
    text = raw_text if isinstance(raw_text, str) else (str(raw_text) if raw_text is not None else '')
    if not text:
        sys.stderr.write('Error: upstream returned empty completion (no response text)\n')
        sys.exit(1)
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
