# Claude Auto Router

Claude Auto Router adds durable Claude Code jobs to Hermes Agent and Codex. It
provides host-native adapters for dispatch, follow-up messages, status,
completion delivery, listing, graceful close, and controlled restart.

The Hermes adapter is the root plugin. The Codex adapter lives in
[`adapters/codex`](adapters/codex/README.md) and runs as a local stdio MCP
server with Codex app-server completion callbacks.

## Before you start

You need Claude Code installed and authenticated on the same machine, plus an
existing local repository or project directory. Then choose either Hermes or
Codex as the coordinator.

The plugin uses the Claude Code model aliases `opus` and `fable`. It intentionally does not offer Sonnet.

## Install for Hermes

Use Hermes's plugin installer so it scans the repository, records its source,
and enables the root plugin explicitly:

```bash
hermes plugins install karthikcsq/claude-auto-router --enable
hermes gateway restart
```

After the gateway restarts, ask Hermes to list active Claude sessions. To
update later, run `hermes plugins update claude-auto-router` and restart the
gateway. The nested `adapters/codex` directory is inert in Hermes: Hermes loads
the root `plugin.yaml` and `__init__.py` as one plugin.

## Install for Codex

Keep the clone at a stable absolute path, then run:

```bash
claude auth status
./adapters/codex/install.sh /absolute/allowed/project/root
```

The installer registers the repository adapter as the global
`claude_delegation` stdio MCP server. It also installs the wake/restart callback
LaunchAgent, writes machine-specific configuration outside the repository, and
merges the routing hook into `~/.codex/hooks.json` without replacing unrelated
hooks. Open a new Codex task, run `/mcp`, and confirm the six tools listed below.

See [the Codex adapter guide](adapters/codex/README.md) for exact configuration,
security boundaries, callback behavior, and tests.

## The basic workflow

1. **Dispatch** a job with a clear task and an existing absolute work directory.
2. **Check status** while it is running.
3. **Send a message** if the requirements change.
4. **Verify the work** in the repository.
5. **Close** the session when no more follow-up is needed.

Example dispatch:

```json
{
  "task": "Add input validation to the account settings form. Add focused tests and run the relevant test suite.",
  "workdir": "/absolute/path/to/project",
  "model": "opus",
  "effort": "high"
}
```

The adapter returns a job ID such as `claude-abc123`. Keep it for the remaining lifecycle actions.

## Available tools

### `claude_code_dispatch`

Starts a durable Claude Code job.

Required fields:

- `task`: the concrete task and acceptance criteria
- `workdir`: an existing absolute path to the project

Useful optional fields:

- `model`: `opus` for most substantial work; `fable` only for unusually broad or long-running work
- `effort`: `high`, `xhigh`, or `max`
- `max_turns`: a maximum number of Claude turns
- `permission_mode`: `acceptEdits` or `auto`
- `dry_run`: validate a dispatch without starting it

Give Claude enough context to work safely: name the repository, the goal, constraints, files or areas to inspect, and the tests that should pass.

### `claude_code_status`

Shows one job's compact supervisor status: current phase, validation, blockers, and next action.

```json
{ "job_id": "claude-abc123" }
```

Use status while a job is active. It is designed to be concise and does not return a full transcript or diff.

### `claude_code_message`

Sends new requirements to an active session without creating a second job.

```json
{
  "job_id": "claude-abc123",
  "message": "Also add a regression test for an empty display name."
}
```

The message is delivered as Claude's next turn after its current turn finishes.

### `claude_code_list`

Lists all active Claude sessions in a compact form. Each entry includes job ID, state, model, work directory, parallel lane, start time, and whether it needs a human decision.

Use this before starting another job when you are unsure what is already running.

### `claude_code_close`

Ends an active session permanently after its final work has been verified.

```json
{ "job_id": "claude-abc123" }
```

Close is intentionally irreversible. Hermes sends EOF and then terminates a
stuck managed process after a short grace. Codex records a durable close request
and lets the in-flight Claude turn finish before the worker exits. Do not close
a job if you expect to send more requirements.

### `claude_code_restart`

Use this only when a non-Fable session was closed too early and must continue with current requirements.

```json
{
  "source_job_id": "claude-abc123",
  "task": "Continue the validation work. Run the full test suite and fix only confirmed failures."
}
```

Restart creates one explicit replacement job with a recorded relationship to the closed job. It does not silently retry failed work, and it does not reopen the old session's stdin.

## Approval requests in Hermes

A job can enter `action_required` when Claude needs a human approval or permission decision. That is not a failure.

Read the reported summary, make the decision yourself, then send your decision to the same job with `claude_code_message`. The router never approves actions on your behalf.

## Running two independent Hermes jobs

The default is one Claude parent job per coordinator turn. For two genuinely independent jobs in the same turn, both dispatches must opt in with different `parallel_lane` names and must use separate isolated checkouts.

```json
{
  "task": "Review the authentication module and report findings only.",
  "workdir": "/projects/app-review-worktree",
  "parallel_lane": "review"
}
```

```json
{
  "task": "Implement the approved documentation updates.",
  "workdir": "/projects/app-docs-worktree",
  "parallel_lane": "docs"
}
```

The router allows at most two such jobs. It rejects duplicate lanes, shared or overlapping directories, and worktrees from the same Git checkout. Parallel lanes are not available for Fable jobs or recovery jobs.

## Fable jobs and recovery

Use `fable` only for very large or unusually complex work. If a Fable job ends incomplete or fails, treat the repository state as needing review before another attempt.

The router deliberately does not automatically retry, downgrade, restart, or fan out a Fable failure. A recovery must be explicitly requested and names the failed job ID through `recovery_of_job_id`. Only one isolated recovery is allowed for a given failed job.

## Development

The tests do not require Claude Code credentials or invoke the live CLI:

```bash
python -m pytest tests -q
```

## Privacy

This repository excludes live job data such as `runs/`, transcripts, cached output, environment files, and tokens. Keep those files out of commits when developing locally.

## License

MIT. See [LICENSE](LICENSE).
