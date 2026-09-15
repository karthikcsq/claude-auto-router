# Codex adapter

This adapter exposes Claude Auto Router as a user-global Codex stdio MCP
server. It uses the same six tool names as the Hermes adapter:

- `claude_code_dispatch`
- `claude_code_status`
- `claude_code_message`
- `claude_code_close`
- `claude_code_list`
- `claude_code_restart`

The implementation is independent of Hermes internals. It uses Codex MCP
metadata to bind each job to the originating task and uses Codex app-server to
deliver terminal events back to that exact task.

## Requirements

- macOS with `launchd` and `/usr/bin/caffeinate`
- Codex CLI with app-server `turn/start.toolOutput` support
- Claude Code CLI installed and authenticated
- one explicit existing directory that contains every allowed workdir

Check the CLIs without printing credentials:

```bash
command -v codex
command -v claude
claude auth status
```

## Install

Keep this clone at a stable absolute path. From the repository root:

```bash
./adapters/codex/install.sh /absolute/allowed/project/root
```

The installer safely inspects an existing `claude_delegation` MCP entry before
updating it. The equivalent registration is:

```bash
codex mcp add claude_delegation \
  --env "CLAUDE_DELEGATION_ALLOWED_ROOTS=/absolute/allowed/project/root" \
  --env "CLAUDE_DELEGATION_STATE_DIR=$HOME/.local/state/claude-delegation-hook" \
  --env "CLAUDE_DELEGATION_CLI=/absolute/path/to/claude" \
  --env "CLAUDE_DELEGATION_CODEX_CLI=/absolute/path/to/codex" \
  --env "CLAUDE_DELEGATION_PREVENT_IDLE_SLEEP=1" \
  -- /usr/bin/python3 /absolute/path/to/claude-auto-router/adapters/codex/claude_code_delegation.py
```

Machine-specific files are written outside the clone:

- `~/.codex/config.toml`: global MCP registration
- `~/.config/claude-auto-router/codex.json`: routing-hook configuration
- `~/.codex/hooks.json`: merged hook registration
- `~/.local/state/claude-delegation-hook`: durable job state and redacted logs
- `~/Library/LaunchAgents/com.openai.codex.claude-delegation-callback.plist`:
  wake, login, and callback recovery

Open a new Codex task after installation. Run `/mcp` and `/hooks` to confirm the
server and routing hook. Existing tasks do not reload their MCP inventory.

## Completion delivery

Dispatch persists the originating Codex task, turn, coordinator identity,
workdir, callback target, and stable event id. A terminal job emits one durable
`claude_delegation_completed` event. Delivery calls `thread/read`, resumes only
the recorded task with `thread/resume`, then starts a Codex turn with a named
`toolOutput` and trusted application context. No fabricated user message is
used.

The resumed coordinator is instructed to inspect the repository and test
results before reporting success. Duplicate completion signals, retries, and
restarts are deduplicated by the durable event marker. Deleted tasks are marked
unavailable and never receive another task's event.

## Sleep and restart behavior

Every worker gets `caffeinate -i -w <worker-pid>`, which prevents idle sleep
without keeping the display awake. Closing the lid still suspends local
processes. A surviving worker continues after wake with the same PID and Claude
session. The LaunchAgent uses a wake-coalescing calendar schedule and
`RunAtLoad` recovery.

If logout or reboot removed the worker, recovery records a truthful terminal
failure and delivers it once. It never replays the task, downgrades Fable, or
switches models.

## Security boundaries

The adapter requires explicit allowed roots and resolves symlinks before every
workdir check. The state directory must be outside those roots. Subprocesses use
argument arrays with `shell=False`; prompts travel over stdin and stay out of
process listings. Dispatch and the detached worker both run `claude auth
status` with output discarded.

State uses owner-only permissions, file locking, and atomic replacement. Raw
prompts and raw Claude output are not written to logs. Bounded logs and retained
validation text are credential-redacted.

`opus` and `fable` are rolling aliases. Numbered Fable 5 and 5.1 pins remain
available. Sonnet is rejected. Failed Fable work is never retried, downgraded,
or fanned out automatically. `claude_code_restart` is limited to one
replacement for a user-closed non-Fable job.

## Verify

```bash
codex mcp get claude_delegation
codex mcp list
launchctl print "gui/$(id -u)/com.openai.codex.claude-delegation-callback"
python -m pytest tests -q
python adapters/codex/e2e_trace.py
```

The tests use fake Claude and Codex processes. They do not dispatch a billable
Claude task.
