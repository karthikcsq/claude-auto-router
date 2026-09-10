#!/usr/bin/env bash
# Register this repository's Codex adapter as a user-global stdio MCP server.
set -euo pipefail

ALLOWED_ROOT="${1:-$HOME/CodingFiles}"
SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
SERVER="$SCRIPT_DIR/claude_code_delegation.py"
ROUTING_HOOK="$SCRIPT_DIR/routing_hook.py"
ROUTING_POLICY="$SCRIPT_DIR/routing-policy.md"
INSTALL_SUPPORT="$SCRIPT_DIR/install_support.py"
STATE_DIR="${CLAUDE_DELEGATION_STATE_DIR:-$HOME/.local/state/claude-delegation-hook}"
HOOK_CONFIG="${CLAUDE_DELEGATION_HOOK_CONFIG:-$HOME/.config/claude-auto-router/codex.json}"
HOOKS_JSON="${CODEX_HOOKS_JSON:-$HOME/.codex/hooks.json}"
PYTHON_BIN="/usr/bin/python3"
NAME="claude_delegation"
PREVENT_IDLE_SLEEP="1"

CODEX_BIN="$(command -v codex)" || { echo "Codex CLI is not on PATH." >&2; exit 1; }
CLAUDE_BIN="$(command -v claude)" || { echo "Claude Code CLI is not on PATH." >&2; exit 1; }
ALLOWED_ROOT="$("$PYTHON_BIN" -c 'from pathlib import Path; import sys; print(Path(sys.argv[1]).expanduser().resolve())' "$ALLOWED_ROOT")"
STATE_DIR="$("$PYTHON_BIN" -c 'from pathlib import Path; import sys; print(Path(sys.argv[1]).expanduser().resolve())' "$STATE_DIR")"
"$PYTHON_BIN" --version >/dev/null
claude auth status >/dev/null 2>&1 || {
  echo "Claude Code is not authenticated; run 'claude auth login' first." >&2
  exit 1
}
[[ -f "$SERVER" ]] || { echo "Missing server: $SERVER" >&2; exit 1; }
[[ -f "$ROUTING_HOOK" ]] || { echo "Missing routing hook: $ROUTING_HOOK" >&2; exit 1; }
[[ -f "$ROUTING_POLICY" ]] || { echo "Missing routing policy: $ROUTING_POLICY" >&2; exit 1; }
[[ -f "$INSTALL_SUPPORT" ]] || { echo "Missing installer support: $INSTALL_SUPPORT" >&2; exit 1; }
[[ -d "$ALLOWED_ROOT" ]] || { echo "Allowed root does not exist: $ALLOWED_ROOT" >&2; exit 1; }
"$PYTHON_BIN" "$SERVER" \
  --allowed-root "$ALLOWED_ROOT" \
  --state-dir "$STATE_DIR" \
  --claude-bin "$CLAUDE_BIN" \
  --codex-bin "$CODEX_BIN" </dev/null

SCHEMA_DIR="$(mktemp -d "${TMPDIR:-/tmp}/claude-delegation-schema.XXXXXX")"
trap 'rm -rf "$SCHEMA_DIR"' EXIT
"$CODEX_BIN" app-server generate-json-schema --experimental --out "$SCHEMA_DIR" >/dev/null
grep -q '"toolOutput"' "$SCHEMA_DIR/v2/TurnStartParams.json" || {
  echo "Codex app-server lacks turn/start toolOutput support; update Codex first." >&2
  exit 1
}

mkdir -p "$STATE_DIR"
chmod 700 "$STATE_DIR"

NEEDS_ADD=1
if "$CODEX_BIN" mcp get "$NAME" >/dev/null 2>&1; then
  if CHECK_OUTPUT="$($CODEX_BIN mcp get "$NAME" --json | "$PYTHON_BIN" -c '
import json, sys
data=json.load(sys.stdin)
expected={
  "command":sys.argv[1],
  "args":[sys.argv[2]],
  "env.CLAUDE_DELEGATION_ALLOWED_ROOTS":sys.argv[3],
  "env.CLAUDE_DELEGATION_STATE_DIR":sys.argv[4],
  "env.CLAUDE_DELEGATION_CLI":sys.argv[5],
  "env.CLAUDE_DELEGATION_CODEX_CLI":sys.argv[6],
  "env.CLAUDE_DELEGATION_PREVENT_IDLE_SLEEP":sys.argv[7],
}
actual={"command":data.get("transport",{}).get("command"),"args":data.get("transport",{}).get("args")}
actual.update({"env."+key:value for key,value in data.get("transport",{}).get("env",{}).items() if key in {"CLAUDE_DELEGATION_ALLOWED_ROOTS","CLAUDE_DELEGATION_STATE_DIR","CLAUDE_DELEGATION_CLI","CLAUDE_DELEGATION_CODEX_CLI","CLAUDE_DELEGATION_PREVENT_IDLE_SLEEP"}})
differences=[key+": current="+repr(actual.get(key))+" expected="+repr(value) for key,value in expected.items() if actual.get(key)!=value]
if differences:
 print("Existing claude_delegation entry differs:\n"+"\n".join(differences))
 raise SystemExit(1)
' "$PYTHON_BIN" "$SERVER" "$ALLOWED_ROOT" "$STATE_DIR" "$CLAUDE_BIN" "$CODEX_BIN" "$PREVENT_IDLE_SLEEP")"; then
    NEEDS_ADD=0
    echo "Codex MCP server '$NAME' already matches; leaving it intact."
  else
    echo "$CHECK_OUTPUT" >&2
    echo "Updating only the inspected '$NAME' entry." >&2
    "$CODEX_BIN" mcp remove "$NAME"
  fi
fi

if [[ "$NEEDS_ADD" -eq 1 ]]; then
  "$CODEX_BIN" mcp add "$NAME" \
    --env "CLAUDE_DELEGATION_ALLOWED_ROOTS=$ALLOWED_ROOT" \
    --env "CLAUDE_DELEGATION_STATE_DIR=$STATE_DIR" \
    --env "CLAUDE_DELEGATION_CLI=$CLAUDE_BIN" \
    --env "CLAUDE_DELEGATION_CODEX_CLI=$CODEX_BIN" \
    --env "CLAUDE_DELEGATION_PREVENT_IDLE_SLEEP=$PREVENT_IDLE_SLEEP" \
    -- "$PYTHON_BIN" "$SERVER"
fi

"$CODEX_BIN" app-server daemon start >/dev/null

LAUNCH_AGENT_TARGET="$HOME/Library/LaunchAgents/com.openai.codex.claude-delegation-callback.plist"
"$PYTHON_BIN" "$INSTALL_SUPPORT" \
  --server "$SERVER" \
  --routing-hook "$ROUTING_HOOK" \
  --policy "$ROUTING_POLICY" \
  --allowed-root "$ALLOWED_ROOT" \
  --state-dir "$STATE_DIR" \
  --claude-bin "$CLAUDE_BIN" \
  --codex-bin "$CODEX_BIN" \
  --launch-agent "$LAUNCH_AGENT_TARGET" \
  --hook-config "$HOOK_CONFIG" \
  --hooks-json "$HOOKS_JSON"
launchctl bootout "gui/$(id -u)" "$LAUNCH_AGENT_TARGET" >/dev/null 2>&1 || true
launchctl bootstrap "gui/$(id -u)" "$LAUNCH_AGENT_TARGET"

"$CODEX_BIN" mcp get "$NAME"
"$CODEX_BIN" mcp list
"$CODEX_BIN" app-server daemon version
launchctl print "gui/$(id -u)/com.openai.codex.claude-delegation-callback" | sed -n '1,18p'
echo "Open a fresh Codex session and use /mcp to confirm claude_delegation is connected."
echo "Tools: claude_code_dispatch, claude_code_status, claude_code_message, claude_code_close, claude_code_list, claude_code_restart"
