# Claude Auto Router for Hermes

A standalone [Hermes Agent](https://hermes-agent.nousresearch.com/) plugin that routes substantial engineering work to Claude Code while preserving durable job lifecycle state.

## What it provides

- `claude_code_dispatch` for durable Claude Code jobs using `opus` or `fable`
- `claude_code_message` for newline-framed follow-up requirements
- `claude_code_status` for compact per-job lifecycle and supervisor state
- `claude_code_close` that terminates the managed process, rather than only closing stdin
- `claude_code_restart` for one explicit replacement of a prematurely closed non-Fable job
- `claude_code_list` for compact active-session inventory
- Opt-in same-turn parallelism: at most two distinct `parallel_lane` values, each in a separate isolated checkout
- Durable terminal reconciliation, safe approval-block reporting, and guarded Fable recovery lineage

## Installation

This plugin requires a working Hermes installation and an authenticated Claude Code CLI.

```bash
git clone https://github.com/karthikcsq/claude-auto-router.git ~/.hermes/plugins/claude-auto-router
hermes gateway restart
```

After restart, a dispatcher tool response should report the plugin version that matches `plugin.yaml`.

## Lifecycle contract

`claude_code_close` is irreversible: it sends EOF, allows a short graceful-exit window, then uses Hermes's managed-process termination rail if needed. A close must never leave a session indefinitely in `closing`.

If a non-Fable session was closed before its work was complete, use `claude_code_restart` with the source job ID and a complete current task. The new job is an explicit, single audited replacement; it is not an automatic retry.

A failed or incomplete Fable job remains a recovery stop. It cannot be retried, downgraded, restarted, or fanned out automatically. Use one explicit named recovery only after reviewing the terminal state.

## Controlled parallelism

The default remains one parent job per coordinator turn. To run two independent jobs in one turn, both dispatches must provide different short `parallel_lane` values and use separate non-overlapping isolated worktrees. Lanes are rejected for Fable and recovery jobs. The plugin rejects same, nested, or shared-Git-worktree paths.

## Development

```bash
python -m pytest tests -q
```

The suite uses a fake Hermes process rail plus real short-lived process-registry coverage. It does not invoke Claude Code or require credentials.

## Security and privacy

The repository deliberately excludes persisted `runs/` data, transcripts, cached output, tokens, environment files, and host-specific paths. Job records on a live installation can contain operational metadata and must not be committed.

## License

MIT. See [LICENSE](LICENSE).
