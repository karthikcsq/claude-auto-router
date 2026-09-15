# Codex routing hook

This global Codex hook injects the Claude routing policy as developer context
through Codex's native `UserPromptSubmit` and `SubagentStart` events. It does
not change system, developer, or user messages directly. Helper model calls do
not emit these events.

Codex does not expose a provider-level `pre_llm_call` or `before_model` event.
`UserPromptSubmit` is the supported point immediately before a coordinator turn
is sent to the model. `SubagentStart` supplies the same mechanism for agent
identities explicitly listed in `target_agents`. The injected developer context
remains in the turn across tool loops and provider retries, while the per-turn
state marker prevents the hook from adding it again.

Completion delivery does not reuse this prompt hook. The MCP server captures
Codex's origin metadata at dispatch and resumes that exact thread through the
native app-server `thread/read`, `thread/resume`, and `turn/start` methods. The
completion arrives as a named tool output plus trusted
application context. The resumed coordinator is told to inspect the repository
and tests before reporting success, then close the job when final handling is
complete.

## Configuration

The installer writes `~/.config/claude-auto-router/codex.json`:

- `enabled`: defaults to `true`.
- `target_agents`: defaults to `["coordinator"]`. Add exact `agent_type` values
  to target named subagents.
- `policy_text`: optional inline override. When non-empty, it takes precedence.
- `policy_file`: points to the repository's `routing-policy.md`.
- `fallback_behavior`: `inject_note` by default; set to `skip` to emit nothing
  when the tools are unavailable.
- `fallback_text`: truthful developer context used by `inject_note`.
- `mcp_server`: defaults to `claude_delegation`.
- `required_tools`: the six adapter tool names that must all be present.
- `codex_bin`, `python_bin`, and `server_path`: absolute local launch paths.
- `allowed_roots` and `state_dir`: mirror the MCP registration.
- `dedupe_ttl_hours`: defaults to `168`.

When the hook event includes a tool list, the hook checks that list directly.
Current Codex hook events do not include one, so the hook verifies the enabled
`claude_delegation` registration and performs a local `initialize` plus
`tools/list` handshake against its configured server. A fresh-session `/mcp`
check remains the definitive connection check for that session.

## Activation

The global `~/.codex/hooks.json` registration applies to new and resumed Codex
sessions. Hooks are enabled by default. Open a new Codex session, run `/hooks`,
review the two entries from this directory, and trust them. Then use `/mcp` to
confirm that `claude_delegation` exposes:

- `claude_code_dispatch`
- `claude_code_status`
- `claude_code_message`
- `claude_code_close`
- `claude_code_list`
- `claude_code_restart`

## Tests

```bash
python -m pytest tests/test_codex_routing_hook.py -q
```

The subprocess trace test runs the real hook entrypoint, captures the augmented
request immediately before a fake provider dispatch, and verifies that the
original messages and tools are unchanged and the marker appears once.
