"""Automatic engineering routing for the default Hermes profile.

The hook is intentionally guidance-only: Hermes retains judgment about task
complexity and authorization. The dispatch tool owns the fragile CLI invocation
and persists a small, inspectable job record for later status checks.
"""

from __future__ import annotations

import json
import os
import re
import shlex
import shutil
import subprocess
import threading
import time
import uuid
from pathlib import Path
from typing import Any

PLUGIN_DIR = Path(__file__).resolve().parent
RUNS_DIR = PLUGIN_DIR / "runs"
CLAUDE = shutil.which("claude") or "claude"


def _plugin_version() -> str:
    """Version from plugin.yaml as it was when this module was imported."""
    try:
        text = (PLUGIN_DIR / "plugin.yaml").read_text(encoding="utf-8")
    except OSError:
        return "unknown"
    match = re.search(r"^version:\s*['\"]?([^'\"\s#]+)", text, re.MULTILINE)
    return match.group(1) if match else "unknown"


# The gateway imports a plugin once per process (hermes_cli.plugins
# PluginManager.discover_and_load is guarded by `_discovered`; nothing watches
# the source for changes), so edits made afterwards are invisible until it
# restarts or force reloads. Every answer and every durable write therefore
# names the code that produced it: a live proof must never again test code
# that is not running without noticing (job claude-bbb95a9ee9a7).
PLUGIN_VERSION = _plugin_version()
PLUGIN_LOADED_AT = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _loaded_code() -> dict[str, str]:
    return {"plugin_version": PLUGIN_VERSION, "plugin_loaded_at": PLUGIN_LOADED_AT}


MAX_TASK_CHARS = 16_000
MAX_STATUS_CHARS = 6_000
MAX_TURNS = {"opus": 120, "fable": 140}
ALLOWED_MODELS = set(MAX_TURNS)
MAX_DISPATCHES_PER_TURN = 1
# Same-turn parallelism is an explicit, bounded opt-in, enforced from the job
# records rather than from the process registry: the registry counts every
# managed process in the gateway, while this cap is about how many parents one
# coordinator turn is allowed to own.
MAX_PARALLEL_PARENTS_PER_TURN = 2
# A lane is a short, stable, human-meaningful label ("analysis",
# "implementation"), not free text: it appears in errors, records and listings.
PARALLEL_LANE_RE = re.compile(r"[a-z0-9][a-z0-9_-]{1,31}")
RECOVERY_FENCE_SECONDS = 24 * 60 * 60
COMPLETION_WATCH_INTERVAL_SECONDS = 5
# `claude_code_close` ends the session. Claude's clean shutdown is stdin EOF, so
# that is still asked for first; this is how long the child gets to take it
# before the close escalates through the registry's terminate/kill API. Short,
# because the coordinator only closes a stream it has already verified.
CLOSE_GRACE_SECONDS = 5.0
# Recorded on the killed session by ProcessRegistry.kill_process, so a terminal
# record can say which component ended the process.
CLOSE_TERMINATION_SOURCE = "claude_auto_router.close"
MAX_TERMINAL_EVIDENCE_CHARS = 400
MAX_ACTION_EVIDENCE_CHARS = 240
MAX_ACTION_TOOLS = 5

# Non-terminal states are the only ones reconciliation is allowed to rewrite.
# `action_required` is live, not terminal: the Claude process is still up and
# the job resumes the moment the originating user answers.
LIVE_STATES = frozenset({
    "starting", "running", "waiting_for_update", "action_required", "closing",
})
# A live job still occupies the single coordinator dispatch slot, whether it is
# working, idle between turns, or waiting on a human approval decision.
ACTIVE_PARENT_STATES = frozenset({"running", "waiting_for_update", "action_required"})
# Terminal states are sticky: once written they are never downgraded by a later
# poll, so a restarted gateway cannot turn a recorded failure back into
# "running" (or a recorded success into "unknown").
TERMINAL_STATES = frozenset({
    "completed_unverified",
    "failed_terminal",
    "incomplete_recoverable",
    "unknown_needs_reconciliation",
    "closed_by_user",
    # Legacy states written by the pre-streaming one-shot implementation.
    "completed",
    "failed",
    "timed_out",
})
# Everything a terminal reconciliation owns. Written together by
# `_finalize_record`, and the only fields a stale handler write may not
# overwrite once the job has ended.
TERMINAL_RECORD_FIELDS = (
    "state",
    "outcome",
    "completed_at",
    "completion_signal",
    "terminal_report",
    "requires_explicit_recovery",
    "recovery_guidance",
    "claude_result",
    "process_status_error",
)
# States that must never be auto-retried, downgraded, or fanned out.
RECOVERY_FENCED_STATES = frozenset({
    "failed",
    "failed_terminal",
    "timed_out",
    "incomplete_recoverable",
    "unknown_needs_reconciliation",
})

ROUTING_POLICY = """
## Automatic engineering routing
Do not ask Karthik whether to use an agent. Make the routing decision yourself.

- Handle genuinely small, safely verifiable requests directly with Terra.
- For moderate implementation/debugging/refactoring work, use `delegate_task` automatically. The configured delegation lane is GPT-5.6 Sol. Prefer an orchestrator role when decomposition materially helps.
- For heavy engineering work — multi-file implementation, difficult debugging, architecture changes, migrations, security-sensitive changes, work requiring independent test/review passes, or a task that already failed a Sol pass — call `claude_code_dispatch` automatically.
- Use Claude **Opus** by default for heavy implementation. Route truly big work — broad multi-module delivery, large refactors, long multi-phase projects, or unusually complex/repeatedly failing work — to Claude **Fable**. Never choose Claude Sonnet.
- The Claude Code parent may use its own project-defined subagents. Do not recreate its leaf-agent orchestration in Hermes unless the task explicitly needs separate independent workstreams.
- Before dispatch, supply the repo/workdir and concrete acceptance criteria. If the user adds requirements while a compatible Claude job is still running, send them with `claude_code_message` instead of killing and redispatching; Claude queues the new turn after its current turn. After a final turn is verified and no further update is expected, call `claude_code_close`: it ends the session for good (EOF, a short grace, then termination of the managed process), so never close a job whose requirements are still open. If a job was closed too early, its stdin cannot be reopened — call `claude_code_restart` with that job id and the current requirements to start the single authorized replacement, and never silently redispatch instead. Use `claude_code_status` only while the background job is still running. Report verified results, not merely that a worker was started.
- If `claude_code_status` reports state `action_required`, Claude is blocked on a human approval/permission decision — it has NOT failed and it is still live. Relay `action_required_detail.summary` to Karthik, never approve on his behalf or auto-approve anything, and send his decision back with `claude_code_message` on the same job id.
- One Claude parent per coordinator turn is still the default. Two parents may share a turn only as a deliberate opt-in: pass a distinct `parallel_lane` on **both** dispatches (e.g. `analysis` and `implementation`), give each lane its own isolated checkout, and stay within the cap of two. Never use lanes for Fable or for a recovery job, and never use them to run two agents over one working tree.
- `claude_code_list` shows the still-active Claude sessions compactly (job id, state, model, workdir, lane, action-required). Prefer it over guessing what is running; use `claude_code_status` for one job's detail.
- A Fable timeout/failure is a recovery stop, not authorization to fan out. Do not automatically retry it, downgrade it to Opus, or create parallel implementation lanes. Report the verified terminal state and wait for Karthik's explicit recovery instruction. A recovery is one isolated job, never a same-turn multi-job fan-out.
""".strip()

# This is attached only to a successful task-plan write, becoming the immediate
# tool result before the coordinator chooses its next action. A post_tool_call
# hook cannot do this because it is observer-only; transform_tool_result can.
TODO_ROUTING_CHECKPOINT = """
[ROUTING CHECKPOINT — plan written]
Before direct implementation, choose the execution lane. Do not ask the user
whether to delegate.
- Small, contained, safely verifiable task: Terra may continue directly.
- Moderate implementation/debugging/refactor: call delegate_task (Sol).
- Heavy multi-file, benchmark, migration, security, difficult debugging, or
  independently-verified work: call claude_code_dispatch with model="opus".
- Broad multi-module, long-running, or unusually complex work: call
  claude_code_dispatch with model="fable".
Do not start direct file mutations for work that belongs in a delegated lane.
""".strip()

DISPATCH_SCHEMA: dict[str, Any] = {
    "name": "claude_code_dispatch",
    "description": (
        "Launch a durable local Claude Code engineering job using the Claude Max CLI. "
        "Use automatically for heavy implementation work. Defaults to Opus; route only big "
        "multi-module, long-running, or unusually complex work to Fable. "
        "Never use Sonnet. The job runs in the specified existing repository/workdir and "
        "returns a job id for claude_code_status. Set dry_run=true only to validate the command."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "task": {"type": "string", "description": "Concrete task plus acceptance criteria."},
            "workdir": {"type": "string", "description": "Existing absolute project directory."},
            "model": {"type": "string", "enum": ["opus", "fable"], "description": "Opus by default; Fable only for big, long-running, or unusually complex work."},
            "effort": {"type": "string", "enum": ["high", "xhigh", "max"], "description": "Reasoning effort; default high for Opus and max for Fable."},
            "max_turns": {"type": "integer", "minimum": 1, "maximum": 200, "description": "Claude agentic turn cap; default 120 for Opus, 140 for Fable."},
            "permission_mode": {"type": "string", "enum": ["acceptEdits", "auto"], "description": "Claude Code permission mode; acceptEdits by default, auto only when autonomous test commands are needed."},
            "recovery_of_job_id": {"type": "string", "description": "Required only for an explicit user-directed recovery of a recently failed Fable job in the same repository. It permits one isolated recovery job, never a fan-out."},
            "parallel_lane": {"type": "string", "description": "Opt-in only. Short stable lane id (2-32 chars, lowercase letters/digits/-/_, e.g. 'analysis' or 'implementation') that lets this turn run a second parent alongside another lane. Both jobs must set distinct lanes and use separate isolated checkouts; the cap is 2 per turn. Not available for Fable or recovery jobs."},
            "dry_run": {"type": "boolean", "description": "Validate and return the exact redacted command without launching Claude."}
        },
        "required": ["task", "workdir"],
        "additionalProperties": False,
    },
}

STATUS_SCHEMA: dict[str, Any] = {
    "name": "claude_code_status",
    "description": "Inspect a Claude Code job using its compact supervisor status file rather than its diff or transcript. Returns lifecycle metadata plus the most recent agent-written phase, validation, blocker, and next-action summary.",
    "parameters": {
        "type": "object",
        "properties": {"job_id": {"type": "string", "description": "Job id returned by claude_code_dispatch."}},
        "required": ["job_id"],
        "additionalProperties": False,
    },
}

MESSAGE_SCHEMA: dict[str, Any] = {
    "name": "claude_code_message",
    "description": (
        "Queue a follow-up requirement for an active Claude Code job without stopping or "
        "redispatching it. Claude receives it as the next user turn after its current turn ends."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "job_id": {"type": "string", "description": "Active job id returned by claude_code_dispatch."},
            "message": {"type": "string", "description": "New requirement, correction, or follow-up acceptance criterion for Claude."},
        },
        "required": ["job_id", "message"],
        "additionalProperties": False,
    },
}

RESTART_SCHEMA: dict[str, Any] = {
    "name": "claude_code_restart",
    "description": (
        "Deliberately replace a Claude Code session that was closed before its work was "
        "finished. Closing a session is irreversible — its stdin cannot be reopened — so this "
        "starts ONE replacement job that reuses the closed job's repository and model and "
        "carries the current requirements you supply. It is not a retry: it refuses a job that "
        "is still live (use claude_code_message), a job that ended on its own, a failed job "
        "(those need an explicit user-directed recovery), a Fable job, and any second "
        "replacement of the same source."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "source_job_id": {"type": "string", "description": "Job id of the closed session being replaced."},
            "task": {"type": "string", "description": "The current, complete requirements for the replacement session plus acceptance criteria. Required: the replacement never silently reuses the old task."},
        },
        "required": ["source_job_id", "task"],
        "additionalProperties": False,
    },
}

LIST_SCHEMA: dict[str, Any] = {
    "name": "claude_code_list",
    "description": (
        "List the Claude Code sessions that are still active, compactly: job id, state, model, "
        "workdir, parallel lane, start time and whether the job is blocked on a human decision. "
        "Finished jobs are omitted. Use it to see what is already running before dispatching, "
        "or to find the job id an approval belongs to; use claude_code_status for one job's detail."
    ),
    "parameters": {"type": "object", "properties": {}, "additionalProperties": False},
}

CLOSE_SCHEMA: dict[str, Any] = {
    "name": "claude_code_close",
    "description": (
        "End a live Claude Code session after its final verified turn: it sends EOF, gives Claude a "
        "short grace to exit on its own, and otherwise terminates the managed process, then records "
        "the terminal outcome. Irreversible — the session can never receive requirements again. "
        "While the job is still needed use claude_code_message instead; if it was closed too early, "
        "claude_code_restart starts the one authorized replacement."
    ),
    "parameters": {
        "type": "object",
        "properties": {"job_id": {"type": "string", "description": "Active job id returned by claude_code_dispatch."}},
        "required": ["job_id"],
        "additionalProperties": False,
    },
}


def _json(value: dict[str, Any]) -> str:
    return json.dumps(value, ensure_ascii=False)


def _record_path(job_id: str) -> Path:
    return RUNS_DIR / f"{job_id}.json"


def _status_path(job_id: str) -> Path:
    return RUNS_DIR / f"{job_id}.status.md"


def _inbox_path(job_id: str) -> Path:
    return RUNS_DIR / f"{job_id}.messages.jsonl"


def _utc_now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _status_file_for(record: dict[str, Any]) -> Path:
    raw_path = str(record.get("status_file") or "")
    return Path(raw_path) if raw_path else _status_path(str(record.get("job_id") or ""))


def _upsert_status_section(path: Path, marker: str, lines: list[str]) -> None:
    """Replace (or append) one Hermes-authored section, never duplicating it.

    The agent owns the rest of the file, so the section is cut at its own
    heading and re-appended rather than rewriting the document.
    """
    try:
        existing = path.read_text(encoding="utf-8")
    except OSError:
        existing = ""
    start = existing.find(marker)
    if start != -1:
        remainder = existing[start + len(marker):]
        following = remainder.find("\n## ")
        existing = existing[:start] + (remainder[following + 1:] if following != -1 else "")
    head = existing.rstrip()[:MAX_STATUS_CHARS]
    block = "\n".join([marker, *lines]) + "\n"
    content = (head + "\n\n" + block) if head else block
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(path.suffix + ".tmp")
        temporary.write_text(content, encoding="utf-8")
        temporary.replace(path)
    except OSError:
        pass


def _write_status_file(path: Path, *, state: str, workdir: Path, model: str, detail: str) -> None:
    """Create a compact, human and agent-readable supervisor status file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    content = (
        f"# Claude supervisor status\n\n"
        f"- **State:** {state}\n"
        f"- **Model:** {model}\n"
        f"- **Workdir:** `{workdir}`\n"
        f"- **Updated (UTC):** {time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())}\n\n"
        f"## Current work\n{detail}\n\n"
        f"## Required compact format\n"
        f"- Current phase and one-sentence objective\n"
        f"- Files/components being changed (names only)\n"
        f"- Latest validation result\n"
        f"- Blocker or next action\n"
    )
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(content)
    temporary.replace(path)


def _append_inbox(path: Path, message: str, *, label: str) -> None:
    """Append an exact, parseable audit event for stream-delivered messages."""
    path.parent.mkdir(parents=True, exist_ok=True)
    event = {
        "schema": "hermes-claude-message/v1",
        "timestamp": time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()),
        "label": label,
        "delivery": "queued",
        "message": message,
    }
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(event, ensure_ascii=False) + "\n")


def _read_status_file(record: dict[str, Any]) -> dict[str, Any]:
    path = _status_file_for(record)
    try:
        content = path.read_text(encoding="utf-8")
        modified_at = path.stat().st_mtime
    except OSError as exc:
        return {"available": False, "path": str(path), "error": str(exc)}
    return {
        "available": True,
        "path": str(path),
        "updated_at": modified_at,
        "truncated": len(content) > MAX_STATUS_CHARS,
        "content": content[:MAX_STATUS_CHARS],
    }


# Hermes has no process-completion plugin hook (see hermes_cli.plugins.VALID_HOOKS),
# so nothing calls back into this plugin when a managed Claude process ends. One
# daemon thread per live job watches the managed session's completion event and
# converges the durable record the moment the process is really gone. It is a
# recorder, never a notifier: the core still owns the single user-facing
# completion callback.
_RECONCILE_LOCK = threading.RLock()
_RECONCILERS: dict[str, threading.Thread] = {}


def _read_record(job_id: str) -> dict[str, Any] | None:
    if not job_id or any(c not in "abcdefghijklmnopqrstuvwxyz0123456789-" for c in job_id):
        return None
    path = _record_path(job_id)
    try:
        value = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return None
    return value if isinstance(value, dict) else None


def _write_record(record: dict[str, Any]) -> None:
    """Atomically persist a job record, durably enough to survive a crash.

    Terminal states are read back by the dispatch guard after a restart, so the
    bytes must be on disk before the rename is visible — a torn or missing
    record would read as "still running" and silently re-open the fence.
    """
    RUNS_DIR.mkdir(parents=True, exist_ok=True)
    target = _record_path(record["job_id"])
    # The staging name must be unique per writer. The exit reconciler, the tool
    # handlers, and a separate `hermes` CLI process can all persist the same job
    # at once; a shared `<job>.tmp` means one writer renames the file out from
    # under another, and the loser dies with FileNotFoundError mid-write.
    temporary = target.with_name(f"{target.name}.{os.getpid()}-{uuid.uuid4().hex[:8]}.tmp")
    payload = json.dumps(record, indent=2, sort_keys=True) + "\n"
    try:
        with temporary.open("w", encoding="utf-8") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        temporary.replace(target)
    except OSError:
        temporary.unlink(missing_ok=True)
        raise
    try:
        directory = os.open(str(RUNS_DIR), os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(directory)
    except OSError:
        pass
    finally:
        os.close(directory)


def _persist_record(record: dict[str, Any]) -> dict[str, Any]:
    """Persist a handler-owned record without clobbering a terminal write.

    The exit reconciler runs concurrently with the tool handlers, so a handler
    that read a live record may be holding a stale snapshot by the time it
    writes. Terminal states are sticky: if the process ended and the reconciler
    finalized the job in between, that outcome wins and this write may only
    carry the handler's live-side fields.
    """
    with _RECONCILE_LOCK:
        current = _read_record(str(record.get("job_id") or ""))
        if (
            current is not None
            and current.get("state") in TERMINAL_STATES
            and record.get("state") not in TERMINAL_STATES
        ):
            for key in TERMINAL_RECORD_FIELDS:
                if key in current:
                    record[key] = current[key]
        _write_record(record)
    return record


def _finalize_record(
    record: dict[str, Any],
    outcome: str,
    *,
    process_status: dict[str, Any],
    result: dict[str, Any],
    output: str,
    completed_at: float | None = None,
    detail: str = "",
) -> dict[str, Any]:
    """Write a sticky terminal state plus its secret-safe explanation, once."""
    if record.get("state") in TERMINAL_STATES:
        return record
    record["state"] = outcome
    record["outcome"] = detail or outcome
    record["completed_at"] = record.get("completed_at") or (completed_at or time.time())
    report = _terminal_report(
        outcome=outcome, process_status=process_status, result=result, output=output
    )
    pending = record.get("action_required")
    pending = pending if isinstance(pending, dict) else {}
    if pending and not pending.get("resolved_at"):
        # The job died still waiting on a human decision. That context is the
        # single most useful thing to hand back, and it must not be silently
        # replaced by the generic terminal summary.
        report["action_required"] = {
            key: pending.get(key)
            for key in ("signal", "confidence", "tools", "summary", "evidence", "detected_at")
        }
        report["summary"] = (
            f"{report.get('summary', '')} It was still blocked awaiting a human approval "
            "decision when it ended; the approval was never granted."
        ).strip()
    record["terminal_report"] = report
    record["completion_signal"] = {
        "kind": "terminal_state",
        "outcome": outcome,
        "watcher_registered": bool(record.get("completion_callback")),
        "recorded_at": report["recorded_at"],
        "plugin_version": PLUGIN_VERSION,
    }
    if outcome in RECOVERY_FENCED_STATES:
        record["requires_explicit_recovery"] = True
        record["recovery_guidance"] = (
            "Do not retry, downgrade, or fan out automatically. Reconcile the repository and wait for an "
            "explicit user-directed recovery that names this job id."
        )
    if result:
        record["claude_result"] = {
            key: result.get(key)
            for key in ("subtype", "session_id", "num_turns", "duration_ms", "stop_reason", "terminal_reason")
        }
    _append_terminal_status(_status_file_for(record), report)
    return record


def _live_process_view(process_session_id: str) -> tuple[dict[str, Any], str]:
    """Return (poll status, full transcript) for a managed process session."""
    if not process_session_id:
        return {}, ""
    try:
        from tools.process_registry import process_registry
    except Exception as exc:  # pragma: no cover - registry always present in Hermes
        # Distinct from not_found: we cannot see the process at all, so we must
        # not convert "cannot look" into "job is gone".
        return {"status": "registry_unavailable", "error": str(exc)}, ""
    process_status = process_registry.poll(process_session_id)
    output = ""
    session = process_registry.get(process_session_id)
    if session is not None:
        with session._lock:
            # Keep the full internal transcript only long enough to locate the
            # last structured result reliably. Nothing raw from it is returned
            # by the tools; supervision is the compact status file.
            output = session.output_buffer
    return process_status, output


def _passive_process_view(process_session_id: str) -> tuple[dict[str, Any], str]:
    """Read a managed session's terminal state without consuming its completion.

    `poll()` records the session in `_poll_observed` and `wait()`/`read_log()`
    record it in `_completion_consumed`; the CLI drain consults the first and
    the gateway/TUI watchers consult the second to suppress a duplicate
    notification. Background reconciliation is a *recorder*, not a consumer —
    taking any of those three entry points would make the core believe the user
    had already been told and silently drop the one completion callback.
    `get()` is the only read with no such side effect, and it still refreshes a
    detached session recovered after a gateway restart.
    """
    if not process_session_id:
        return {}, ""
    try:
        from tools.process_registry import process_registry
        session = process_registry.get(process_session_id)
    except Exception as exc:  # pragma: no cover - registry always present in Hermes
        # Distinct from not_found: we cannot look, so we must not conclude the
        # job is gone.
        return {"status": "registry_unavailable", "error": str(exc)}, ""
    if session is None:
        return {"status": "not_found", "error": f"No process with ID {process_session_id}"}, ""
    with session._lock:
        output = session.output_buffer
    process_status: dict[str, Any] = {
        "session_id": session.id,
        "status": "exited" if session.exited else "running",
    }
    if session.exited:
        process_status["exit_code"] = session.exit_code
        process_status["completion_reason"] = session.completion_reason
        process_status["termination_source"] = session.termination_source
    return process_status, output


def _reconcile_record(
    record: dict[str, Any], view: tuple[dict[str, Any], str] | None = None
) -> dict[str, Any]:
    """Bring one non-terminal record in line with the real process state.

    Called from every tool entry point so a job cannot stay `running`/`closing`
    forever just because nobody polled it, and so a restart that lost the
    managed session resolves to `unknown` rather than a false `running`.
    `view` lets a caller that already polled reuse that snapshot.
    """
    if record.get("state") in TERMINAL_STATES:
        return record
    process_session_id = str(record.get("process_session_id") or "").strip()
    if not process_session_id:
        return record
    process_status, output = view if view is not None else _live_process_view(process_session_id)
    status = process_status.get("status")
    if status == "registry_unavailable":
        return record
    if status == "running":
        last_result = _last_stream_result(output)
        if last_result:
            record["last_turn_result"] = {
                key: last_result.get(key)
                for key in ("subtype", "session_id", "num_turns", "duration_ms", "stop_reason", "terminal_reason")
            }
        # `closing` outranks everything: stdin is already shut, so the job is
        # winding down and must not look like a live session accepting
        # follow-ups.
        if record.get("state") != "closing":
            _apply_action_required(record, output, last_result)
        return record
    if status == "exited":
        result = _last_stream_result(output)
        outcome = _classify_terminal_outcome(
            output,
            result,
            exit_code=process_status.get("exit_code"),
            completion_reason=str(process_status.get("completion_reason") or "exited"),
            observed=True,
        )
        detail = ""
        if record.get("close_forced_at") and outcome != "closed_by_user":
            # The coordinator's own close had to terminate this process. That is
            # neither a Claude failure (it must not arm the recovery fence) nor a
            # success (a result event proves nothing once we cut the run short).
            # The classification it would have had is kept as audit detail.
            detail = f"closed_by_user_after_forced_termination({outcome})"
            outcome = "closed_by_user"
        return _finalize_record(
            record,
            outcome,
            process_status=process_status,
            result=result,
            output=output,
            detail=detail,
        )
    # not_found: the managed session is gone (gateway restart without recovery,
    # or registry pruning). Never infer success from a previously seen result
    # event — date the outcome at last known activity so a stale record cannot
    # spuriously arm the 24h recovery fence with a timestamp of "now".
    record["process_status_error"] = process_status.get("error")
    last_known = float(
        record.get("closed_at")
        or record.get("last_message_queued_at")
        or record.get("started_at")
        or time.time()
    )
    if record.get("state") == "closing":
        # The coordinator had already verified the final turn and closed the
        # stream; a missing process afterwards is the expected end, not an
        # unexplained failure, and must not arm the recovery fence.
        return _finalize_record(
            record,
            "closed_by_user",
            process_status=process_status,
            result={},
            output="",
            completed_at=last_known,
            detail="closed_by_user_after_missing_process",
        )
    return _finalize_record(
        record,
        _classify_terminal_outcome("", {}, observed=False),
        process_status=process_status,
        result={},
        output="",
        completed_at=last_known,
    )


def _reconcile_persisted_record(
    job_id: str, view: tuple[dict[str, Any], str] | None = None
) -> dict[str, Any] | None:
    """Re-read, reconcile and persist one record under the reconcile lock.

    The reconciler thread and the tool handlers can both reach a record at the
    same time, so the background path always re-reads from disk instead of
    reconciling a snapshot it captured earlier — otherwise a queued follow-up
    or a resolved approval written between arming and exit would be silently
    rolled back.
    """
    with _RECONCILE_LOCK:
        record = _read_record(job_id)
        if record is None or record.get("state") in TERMINAL_STATES:
            return record
        before = dict(record)
        _reconcile_record(record, view)
        if record == before:
            return record
        try:
            _write_record(record)
        except OSError:
            pass
        return record


def _annotate_record(job_id: str, updates: dict[str, Any]) -> dict[str, Any] | None:
    """Merge audit-only fields into a record without disturbing its outcome.

    Re-reads under the reconcile lock so a concurrent terminal write is never
    rolled back by a stale snapshot; a `None` value removes the key.
    """
    with _RECONCILE_LOCK:
        record = _read_record(job_id)
        if record is None:
            return None
        for key, value in updates.items():
            if value is None:
                record.pop(key, None)
            else:
                record[key] = value
        try:
            _write_record(record)
        except OSError:
            pass
        return record


def _restart_action(record: dict[str, Any]) -> str | None:
    """The concise restart state a coordinator has to act on, if any."""
    if not isinstance(record.get("restart_pending"), dict):
        return None
    if record.get("restarted_by_job_id"):
        return None
    if record.get("state") in TERMINAL_STATES:
        return "restart_ready"
    # The source is still terminating: nothing was restarted, and saying
    # otherwise would invent a replacement that does not exist.
    return "restart_pending_source_exit"


def _reconciler_loop(job_id: str, process_session_id: str) -> None:
    """Block on the managed session's exit, then write the terminal record."""
    try:
        from tools.process_registry import process_registry
    except Exception:  # pragma: no cover - registry always present in Hermes
        return
    while True:
        record = _read_record(job_id)
        if record is None or record.get("state") in TERMINAL_STATES:
            return
        try:
            session = process_registry.get(process_session_id)
        except Exception:
            return
        if session is None:
            # This process saw the session and it has since disappeared
            # (registry pruning, or a recovery that could not re-attach). That
            # is a real ending, so let the not_found branch date and classify
            # it rather than leaving the record live forever.
            _reconcile_persisted_record(job_id, _passive_process_view(process_session_id))
            return
        if session.exited:
            _reconcile_persisted_record(job_id, _passive_process_view(process_session_id))
            return
        # Set by ProcessRegistry._move_to_finished, the same place the single
        # user notification is enqueued — so this wakes on the real exit rather
        # than on a poll interval. The timeout only bounds a lost wakeup.
        session._completion_event.wait(COMPLETION_WATCH_INTERVAL_SECONDS)


def _arm_record_reconciler(job_id: str, process_session_id: str) -> bool:
    """Ensure exactly one exit watcher per live job in this process.

    Returns False — and starts nothing — when the managed session is not
    visible here. A short-lived `hermes` CLI process reads records written by
    the gateway but owns a different registry instance; speculating about a
    session it cannot see would finalize a job that is still running elsewhere.
    The process that owns the session arms its own reconciler.
    """
    if not job_id or not process_session_id:
        return False
    try:
        from tools.process_registry import process_registry
        session = process_registry.get(process_session_id)
    except Exception:  # pragma: no cover - registry always present in Hermes
        return False
    if session is None:
        return False
    with _RECONCILE_LOCK:
        for done in [key for key, worker in _RECONCILERS.items() if not worker.is_alive()]:
            if done != job_id:
                _RECONCILERS.pop(done, None)
        existing = _RECONCILERS.get(job_id)
        if existing is not None and existing.is_alive():
            return True
        worker = threading.Thread(
            target=_reconciler_loop,
            args=(job_id, process_session_id),
            name=f"claude-reconcile:{job_id}",
            daemon=True,
        )
        _RECONCILERS[job_id] = worker
        worker.start()
    return True


def _repo_identity(workdir: Path) -> str:
    """Stable identity shared by git worktrees; fall back to the real path."""
    try:
        completed = subprocess.run(
            ["git", "-C", str(workdir), "rev-parse", "--path-format=absolute", "--git-common-dir"],
            check=True,
            capture_output=True,
            text=True,
            timeout=3,
        )
        common_dir = completed.stdout.strip()
        if common_dir:
            return str(Path(common_dir).resolve())
    except (OSError, subprocess.SubprocessError):
        pass
    return str(workdir.resolve())


def _all_records() -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for path in RUNS_DIR.glob("claude-*.json"):
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if isinstance(value, dict):
            records.append(value)
    return records


def _reconcile_records(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Reconcile every non-terminal record before the guard reads its state.

    Without this a job that died while nobody was polling stays `running`
    forever: it keeps occupying the one-parent-per-turn slot and, worse, a
    failed Fable campaign never arms the recovery fence.
    """
    for record in records:
        if record.get("state") in TERMINAL_STATES or not record.get("job_id"):
            continue
        before = dict(record)
        try:
            _reconcile_record(record)
        except Exception:
            continue
        if record != before:
            try:
                _persist_record(record)
            except OSError:
                pass
        if record.get("state") not in TERMINAL_STATES:
            # Still live after the sweep: re-arm the exit watcher, so a gateway
            # restart that recovered the managed session also recovers the
            # record's path to a terminal state.
            _arm_record_reconciler(
                str(record.get("job_id") or ""),
                str(record.get("process_session_id") or ""),
            )
    return records


def _lane_of(record: dict[str, Any]) -> str:
    return str(record.get("parallel_lane") or "").strip()


def _lane_label(record: dict[str, Any]) -> str:
    lane = _lane_of(record)
    return f"{record.get('job_id')} (lane {lane})" if lane else str(record.get("job_id"))


def _shared_checkout_reason(
    workdir: Path | None, repo_id: str, existing: dict[str, Any]
) -> str | None:
    """Why two same-turn parents would edit one checkout, or None if isolated.

    Two agents in the same working tree corrupt each other's edits, so this
    fails closed: anything it cannot prove to be a separate checkout counts as
    shared.
    """
    if workdir is None:
        return "the incoming workdir could not be resolved"
    other_raw = str(existing.get("workdir") or "").strip()
    if not other_raw:
        return "its workdir was never recorded, so isolation cannot be proven"
    try:
        other = Path(other_raw).expanduser().resolve()
    except OSError:
        return "its workdir could not be resolved, so isolation cannot be proven"
    if workdir == other:
        return "the same workdir"
    if workdir in other.parents or other in workdir.parents:
        return "an overlapping workdir (one path is inside the other)"
    if repo_id and repo_id == str(existing.get("repo_id") or "").strip():
        # `git rev-parse --git-common-dir` is shared by every linked worktree of
        # one repository, so different paths can still be one checkout's object
        # store, index lineage and branch namespace.
        return "the same git repository (identical git common directory)"
    return None


def _parallel_turn_guard(
    same_turn: list[dict[str, Any]],
    *,
    parallel_lane: str,
    workdir: Path | None,
    repo_id: str,
) -> str | None:
    """Decide whether one more parent may join a turn that already has one."""
    if not parallel_lane:
        return (
            f"dispatch blocked: this coordinator turn already launched {same_turn[0].get('job_id')}. "
            "One parent job per turn is enforced; let that parent use its own subagents. "
            "Two parents may share a turn only when both opt in with distinct parallel_lane "
            "values over separate isolated checkouts."
        )
    laneless = [existing for existing in same_turn if not _lane_of(existing)]
    if laneless:
        return (
            f"dispatch blocked: {laneless[0].get('job_id')} owns this coordinator turn and was "
            "dispatched without a parallel_lane. Parallelism is a two-sided opt-in, so this turn "
            "stays single-parent; wait for that job or start the parallel pair in a new turn."
        )
    active = ", ".join(_lane_label(existing) for existing in same_turn)
    if len(same_turn) >= MAX_PARALLEL_PARENTS_PER_TURN:
        return (
            f"dispatch blocked: this coordinator turn already runs {len(same_turn)} parallel "
            f"parents ({active}); the same-turn cap is {MAX_PARALLEL_PARENTS_PER_TURN}. "
            "Wait for one of them to finish before adding another lane."
        )
    duplicate = next(
        (existing for existing in same_turn if _lane_of(existing) == parallel_lane), None
    )
    if duplicate is not None:
        return (
            f"dispatch blocked: lane '{parallel_lane}' is already held by {duplicate.get('job_id')} "
            "in this coordinator turn. Give the second parent its own distinct lane."
        )
    for existing in same_turn:
        reason = _shared_checkout_reason(workdir, repo_id, existing)
        if reason is not None:
            return (
                f"dispatch blocked: lane '{parallel_lane}' would share {reason} with "
                f"{_lane_label(existing)}. Parallel lanes must each get a separate isolated "
                "checkout with its own git directory (a distinct clone or worktree); two agents "
                "must never edit one working tree."
            )
    return None


def _dispatch_guard(
    *,
    origin_task_id: str,
    repo_id: str,
    recovery_of_job_id: str,
    parallel_lane: str = "",
    workdir: Path | None = None,
    restart_of_job_id: str = "",
) -> str | None:
    """Prevent automatic fallback/fan-out after a parent campaign fails."""
    now = time.time()
    records = _reconcile_records(_all_records())
    # A prior parent only occupies the coordinator turn while it is actually
    # live.  Finished/closing records are historical audit artifacts; a
    # gateway retry or later inbound Discord message can otherwise inherit
    # the same task id and be blocked forever.
    same_turn = [
        existing for existing in records
        if origin_task_id
        and existing.get("origin_task_id") == origin_task_id
        and existing.get("state") in ACTIVE_PARENT_STATES
    ]
    if same_turn:
        # Deterministic order: the error text names the same job first every time.
        same_turn.sort(key=lambda item: (float(item.get("started_at") or 0), str(item.get("job_id") or "")))
        blocked = _parallel_turn_guard(
            same_turn, parallel_lane=parallel_lane, workdir=workdir, repo_id=repo_id
        )
        if blocked:
            return blocked

    recent_failures = []
    for existing in records:
        if existing.get("repo_id") != repo_id or existing.get("model") != "fable":
            continue
        if existing.get("state") not in RECOVERY_FENCED_STATES:
            continue
        failed_at = float(existing.get("completed_at") or existing.get("started_at") or 0)
        if now - failed_at <= RECOVERY_FENCE_SECONDS:
            recent_failures.append((failed_at, str(existing.get("job_id") or ""), existing))

    if not recent_failures:
        if recovery_of_job_id:
            return (
                f"dispatch blocked: recovery id {recovery_of_job_id} is not a recent failed Fable job "
                "in this repository; the 24-hour recovery fence cannot authorize it."
            )
        return None

    _, failed_job_id, _ = max(recent_failures, key=lambda item: (item[0], item[1]))
    if recovery_of_job_id != failed_job_id:
        return (
            f"dispatch blocked: {failed_job_id} is the latest failed Fable job in this repository. "
            "Do not automatically retry, downgrade, or fan out. Report the verified failure and wait for an "
            "explicit user-directed recovery that names this latest job id."
        )

    for existing in records:
        if restart_of_job_id and str(existing.get("job_id") or "") == restart_of_job_id:
            # The job being replaced is the authorized recovery itself; its one
            # replacement continues that chain instead of branching it.
            continue
        if str(existing.get("recovery_of_job_id") or "") == failed_job_id:
            return (
                f"dispatch blocked: Fable job {failed_job_id} already has recovery job "
                f"{existing.get('job_id')}. One failed parent permits exactly one isolated recovery; "
                "do not create an alternate or parallel recovery."
            )
    return None


def _validate_args(
    args: dict[str, Any],
) -> tuple[str, Path, str, str, int, str, str] | str:
    task = str(args.get("task") or "").strip()
    if not task:
        return "task is required"
    if len(task) > MAX_TASK_CHARS:
        return f"task exceeds {MAX_TASK_CHARS} characters"
    workdir = Path(str(args.get("workdir") or "")).expanduser()
    if not workdir.is_absolute() or not workdir.is_dir():
        return "workdir must be an existing absolute directory"
    model = str(args.get("model") or "opus").lower()
    if model not in ALLOWED_MODELS:
        return "model must be opus or fable; Sonnet is intentionally unavailable"
    effort = str(args.get("effort") or ("max" if model == "fable" else "high")).lower()
    if effort not in {"high", "xhigh", "max"}:
        return "effort must be high, xhigh, or max"
    try:
        max_turns = int(args.get("max_turns") or MAX_TURNS[model])
    except (TypeError, ValueError):
        return "max_turns must be an integer"
    if not 1 <= max_turns <= 200:
        return "max_turns must be between 1 and 200"
    permission_mode = str(args.get("permission_mode") or "acceptEdits")
    if permission_mode not in {"acceptEdits", "auto"}:
        return "permission_mode must be acceptEdits or auto"
    # An explicitly supplied lane must be a real lane. Silently reading `""` as
    # "no lane" would turn a caller's mistake into a surprise serial dispatch.
    parallel_lane = ""
    if args.get("parallel_lane") is not None:
        parallel_lane = str(args.get("parallel_lane")).strip()
        if not PARALLEL_LANE_RE.fullmatch(parallel_lane):
            return (
                "parallel_lane must be a short stable identifier: 2-32 characters of "
                "lowercase letters, digits, '-' or '_', e.g. 'analysis' or 'implementation'"
            )
        if model == "fable":
            return (
                "parallel_lane is not available for Fable: a Fable parent is always the only "
                "parent in its coordinator turn. Drop parallel_lane, or use opus lanes."
            )
    return task, workdir.resolve(), model, effort, max_turns, permission_mode, parallel_lane


def _stream_user_message(message: str) -> str:
    """Encode one Claude Agent SDK stream-json user event."""
    return json.dumps({
        "type": "user",
        "message": {"role": "user", "content": message},
        "parent_tool_use_id": None,
    }, ensure_ascii=False)


def _stream_user_event_line(message: str) -> str:
    """Frame one stream-json user event as exactly one delimited line.

    Claude's ``--input-format stream-json`` is newline-delimited JSON: an event
    is only consumed once its terminating newline arrives, and a blank line is
    an empty event. The launch pipe frames its first event explicitly with
    ``printf '%s\n'``; a queued follow-up must be framed the same way here
    rather than inheriting whatever line ending the transport's interactive
    "press Enter" helper appends (``\r\n`` on a Windows PTY, nothing at all on
    a raw write). ``json.dumps`` escapes embedded newlines, so this is always
    exactly one line.
    """
    return _stream_user_message(message) + "\n"


def _submit_stream_event(registry: Any, process_session_id: str, line: str) -> dict[str, Any]:
    """Deliver one already-framed stream-json line without doubling its delimiter."""
    write_raw = getattr(registry, "write_stdin", None)
    if callable(write_raw):
        return write_raw(process_session_id, line)
    # Legacy rail exposing only the "press Enter" helper: it appends the line
    # ending itself, so hand it the payload without ours.
    return registry.submit_stdin(process_session_id, line.rstrip("\n"))


def _stream_events(output: str) -> list[dict[str, Any]]:
    """Every parseable stream-json event in a transcript, in order."""
    events: list[dict[str, Any]] = []
    for line in output.splitlines():
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(event, dict):
            events.append(event)
    return events


def _last_stream_result(output: str) -> dict[str, Any]:
    """Extract the most recent result event from a stream-json/PTY transcript."""
    result: dict[str, Any] = {}
    for line in output.splitlines():
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(event, dict) and event.get("type") == "result":
            result = event
    return result


def _classify_terminal_outcome(
    output: str,
    result: dict[str, Any],
    *,
    exit_code: Any = None,
    completion_reason: str = "exited",
    observed: bool = True,
) -> str:
    """Classify Claude terminal state without trusting exit code/subtype alone.

    A result event alone is not success (the wrapper can still die afterwards),
    and a zero exit code alone is not success (a `-p` run that produced no
    result event tells us nothing). Both must agree before a job is even
    called *completed_unverified* — verification of the repository is still
    the coordinator's job.
    """
    text = output.lower()
    incomplete_signals = (
        "background tasks still running after",
        "workers still running",
        "system-killed",
        "no code/docs changes or prs",
        "no code or prs",
    )
    if any(signal in text for signal in incomplete_signals):
        return "incomplete_recoverable"
    if completion_reason in {"killed", "lost", "failed_start"}:
        return "failed_terminal"
    if not observed:
        # The managed process is gone and its transcript is unavailable, so no
        # amount of exit metadata can prove anything about the repository.
        return "unknown_needs_reconciliation"
    dirty_exit = exit_code is not None and exit_code != 0
    if not result:
        return "failed_terminal" if dirty_exit else "unknown_needs_reconciliation"
    if result.get("subtype") == "success":
        return "failed_terminal" if dirty_exit else "completed_unverified"
    return "failed_terminal"


def _redact(text: str) -> str:
    """Best-effort secret redaction; never return raw text when unavailable."""
    if not text:
        return ""
    try:
        from agent.redact import redact_sensitive_text
    except Exception:
        return "[evidence withheld: redaction unavailable]"
    try:
        return redact_sensitive_text(text, force=True, redact_url_credentials=True)
    except Exception:
        return "[evidence withheld: redaction failed]"


def _terminal_evidence(output: str) -> str:
    """A short, redacted, non-JSON tail explaining a no-result failure."""
    if not output:
        return ""
    lines = [
        line.strip()
        for line in output.splitlines()
        # Stream-json events are structured transcript, not diagnostics; the
        # useful signal for a no-result failure is on stderr-ish plain lines.
        if line.strip() and not line.lstrip().startswith("{")
    ]
    if not lines:
        return ""
    tail = "\n".join(lines[-6:])[-MAX_TERMINAL_EVIDENCE_CHARS:]
    return _redact(tail)


# --------------------------------------------------------------------------
# Action-required (approval-needed) normalization
#
# A turn that ends blocked on a human approval decision is neither a normal
# `waiting_for_update` nor a failure, and burying it in Claude's free text
# means the originating user never learns what is being asked of them. The
# normalizer below is pure: given the parsed stream-json events and the last
# result event it returns a compact, secret-safe description or None.
#
# Structured signals win. Text detection is a deliberately narrow fallback
# scoped to Claude's own final turn text, because task prose routinely
# *discusses* approvals without being blocked by one.
# --------------------------------------------------------------------------

PERMISSION_REQUEST_SUBTYPES = frozenset({"can_use_tool", "permission_request"})

ACTION_REQUIRED_GUIDANCE = (
    "Hermes has NOT granted this permission and must never auto-approve it. Relay the summary to "
    "the originating user and, if they decide to allow it, send their decision with "
    "claude_code_message on this same job id. The job stays live in the meantime."
)
ACTION_REQUIRED_SUMMARY = (
    "Claude reported that it is blocked and needs a human approval decision before it can continue."
)
ACTION_REQUIRED_MARKER = "## Action required (Hermes supervisor)"

_APPROVAL_NOUN = r"(?:approvals?|permissions?|authoriz\w+|authoris\w+|sign-?offs?|consent)"

# Unambiguous: Claude is stating a live block, not describing one.
_STRONG_BLOCK_PATTERNS = tuple(re.compile(pattern, re.IGNORECASE) for pattern in (
    r"\brequested permissions? to use\b",
    r"\bpermissions? (?:was |were |is |are )?denied\b",
    r"\bblocked (?:on|by|pending|awaiting|until)\b[^.!?\n]{0,80}?" + _APPROVAL_NOUN,
    r"\b(?:i|we)(?:'m|'re| am| are)?\s+blocked\b[^.!?\n]{0,120}?" + _APPROVAL_NOUN,
))
# Suggestive: accepted only when the same sentence is not hypothetical or past.
_WEAK_BLOCK_PATTERNS = tuple(re.compile(pattern, re.IGNORECASE) for pattern in (
    r"\b(?:needs?|requires?|awaits?|awaiting|waiting for|requesting|asking for)\s+"
    r"(?:(?:your|explicit|user|human|manual|written|prior|the user's|the user’s)\s+){1,3}"
    + _APPROVAL_NOUN,
    r"\bplease\s+(?:approve|authoriz\w+|authoris\w+|grant\s+(?:me\s+)?" + _APPROVAL_NOUN + r")",
    r"\b" + _APPROVAL_NOUN + r"\s+(?:is|are)\s+required\s+(?:before|to|for)\b",
    r"\b(?:cannot|can ?not|can't|unable to)\s+(?:proceed|continue|complete|finish|go further)\b"
    r"[^.!?\n]{0,80}?\bwithout\b[^.!?\n]{0,40}?" + _APPROVAL_NOUN,
))
# Discussion, hypotheticals, and already-settled approvals are not blocks.
_HYPOTHETICAL_PATTERNS = tuple(re.compile(pattern, re.IGNORECASE) for pattern in (
    r"\b(?:once|after|when|if|unless)\b[^.!?\n]{0,40}?" + _APPROVAL_NOUN,
    r"\b" + _APPROVAL_NOUN + r"\b[^.!?\n]{0,40}?\b(?:was|were|has been|have been|already)\s+"
    r"(?:granted|given|approved|received|obtained)",
    r"\bno\s+" + _APPROVAL_NOUN + r"\s+(?:is\s+|are\s+)?(?:needed|required)",
    r"\b(?:does not|doesn't|do not|don't|did not|didn't)\s+(?:need|require)\b"
    r"[^.!?\n]{0,20}?" + _APPROVAL_NOUN,
    r"\bwithout\s+(?:needing|requiring)\b[^.!?\n]{0,20}?" + _APPROVAL_NOUN,
))
_SENTENCE_SPLIT = re.compile(r"(?<=[.!?])\s+|\n+")
_UNSAFE_TOOL_CHARS = re.compile(r"[^A-Za-z0-9_.:-]")


def _sentences(text: str) -> list[str]:
    return [part.strip() for part in _SENTENCE_SPLIT.split(text or "") if part.strip()]


def _blocking_sentence(text: str) -> str:
    """The one sentence, if any, in which Claude states a live approval block."""
    sentences = _sentences(text)
    for sentence in sentences:
        if any(pattern.search(sentence) for pattern in _STRONG_BLOCK_PATTERNS):
            return sentence
    for sentence in sentences:
        if not any(pattern.search(sentence) for pattern in _WEAK_BLOCK_PATTERNS):
            continue
        if any(pattern.search(sentence) for pattern in _HYPOTHETICAL_PATTERNS):
            continue
        return sentence
    return ""


def _safe_tool_names(values: Any) -> list[str]:
    """Tool identifiers only — never free text pulled out of a transcript."""
    names: list[str] = []
    for value in values:
        name = _UNSAFE_TOOL_CHARS.sub("", str(value or ""))[:48]
        if name and name not in names:
            names.append(name)
        if len(names) >= MAX_ACTION_TOOLS:
            break
    return names


def _last_result_index(events: list[dict[str, Any]]) -> int:
    index = -1
    for position, event in enumerate(events):
        if event.get("type") == "result":
            index = position
    return index


def _turn_text(events: list[dict[str, Any]], result: dict[str, Any]) -> str:
    """Claude's own text for the latest turn; never the task or tool output."""
    text = result.get("result") if isinstance(result, dict) else None
    if isinstance(text, str) and text.strip():
        return text
    for event in reversed(events):
        if event.get("type") != "assistant":
            continue
        message = event.get("message")
        content = message.get("content") if isinstance(message, dict) else None
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            blocks = [
                block.get("text") for block in content
                if isinstance(block, dict) and isinstance(block.get("text"), str)
            ]
            if blocks:
                return "\n".join(blocks)
    return ""


def _action_required_detail(
    signal: str, confidence: str, tools: list[str], evidence: str
) -> dict[str, Any]:
    """Build the user-safe report. The summary is Hermes-authored, not echoed."""
    summary = ACTION_REQUIRED_SUMMARY
    if tools:
        summary += " Awaiting an approval decision for: " + ", ".join(tools) + "."
    detail: dict[str, Any] = {
        "blocked": True,
        "signal": signal,
        "confidence": confidence,
        "tools": tools,
        "summary": summary,
        "guidance": ACTION_REQUIRED_GUIDANCE,
        # Stated explicitly so no downstream reader can infer otherwise.
        "permission_granted": False,
        "auto_approved": False,
    }
    excerpt = " ".join((evidence or "").split())[:MAX_ACTION_EVIDENCE_CHARS]
    if excerpt:
        detail["evidence"] = _redact(excerpt)
    return detail


def _normalize_action_required(
    events: list[dict[str, Any]] | None, result: dict[str, Any] | None
) -> dict[str, Any] | None:
    """Pure: decide whether the latest Claude turn is blocked on an approval."""
    parsed = [event for event in (events or []) if isinstance(event, dict)]
    last_result = result if isinstance(result, dict) else {}

    denials = last_result.get("permission_denials")
    if isinstance(denials, list) and denials:
        return _action_required_detail(
            "permission_denials",
            "structured",
            _safe_tool_names(
                entry.get("tool_name") if isinstance(entry, dict) else entry
                for entry in denials
            ),
            "",
        )

    # A permission request that predates the last result was already answered;
    # only a request still outstanding at the end of the transcript blocks.
    pending = [
        event["request"]
        for event in parsed[_last_result_index(parsed) + 1:]
        if event.get("type") == "control_request"
        and isinstance(event.get("request"), dict)
        and event["request"].get("subtype") in PERMISSION_REQUEST_SUBTYPES
    ]
    if pending:
        return _action_required_detail(
            "control_request",
            "structured",
            _safe_tool_names(request.get("tool_name") for request in pending),
            "",
        )

    sentence = _blocking_sentence(_turn_text(parsed, last_result))
    if sentence:
        return _action_required_detail("result_text", "text", [], sentence)
    return None


def _result_fingerprint(result: dict[str, Any] | None) -> str:
    """Identity of a turn result, so a *newer* turn can clear a stale block."""
    if not isinstance(result, dict) or not result:
        return ""
    return "|".join(
        str(result.get(key))
        for key in ("uuid", "session_id", "num_turns", "duration_ms", "subtype")
    )


def _write_action_required_status(path: Path, detail: dict[str, Any]) -> None:
    """Record the block compactly in the supervisor status file, once."""
    lines = [
        "- **State:** action_required — the job is live and NOT failed.",
        f"- **Signal:** {detail.get('signal')} ({detail.get('confidence')} confidence)",
        f"- **Summary:** {detail.get('summary')}",
    ]
    if detail.get("tools"):
        lines.append(f"- **Awaiting approval for:** {', '.join(detail['tools'])}")
    if detail.get("evidence"):
        lines.append(f"- **Redacted excerpt:** {detail['evidence']}")
    lines.append(f"- **Detected (UTC):** {detail.get('detected_at')}")
    if detail.get("resolved_at"):
        lines.append(
            f"- **Resolved (UTC):** {detail['resolved_at']} via {detail.get('resolved_by')}"
        )
    lines.append("- Hermes did not grant this permission and will not auto-approve it.")
    _upsert_status_section(path, ACTION_REQUIRED_MARKER, lines)


def _apply_action_required(
    record: dict[str, Any], output: str, last_result: dict[str, Any]
) -> None:
    """Set, hold, or clear the sticky action-required state of a live job."""
    detected = _normalize_action_required(_stream_events(output), last_result)
    fingerprint = _result_fingerprint(last_result)
    current = record.get("action_required")
    current = current if isinstance(current, dict) else {}
    unresolved = bool(current) and not current.get("resolved_at")

    if detected:
        if not unresolved or current.get("fingerprint") != fingerprint:
            detail = {**detected, "fingerprint": fingerprint, "detected_at": _utc_now()}
            record["action_required"] = detail
            _write_action_required_status(_status_file_for(record), detail)
        record["state"] = "action_required"
        return
    if unresolved:
        if current.get("fingerprint") == fingerprint:
            # Nothing newer has happened, so the block still stands. Only a
            # later turn or a user follow-up may clear it.
            record["state"] = "action_required"
            return
        resolved = {
            **current, "resolved_at": _utc_now(), "resolved_by": "later_turn_result",
        }
        record["action_required"] = resolved
        _write_action_required_status(_status_file_for(record), resolved)
    record["state"] = "waiting_for_update" if last_result else "running"


def _resolve_action_required(record: dict[str, Any], reason: str) -> bool:
    """Clear an outstanding block and persist the transition. Returns whether one existed."""
    current = record.get("action_required")
    current = current if isinstance(current, dict) else {}
    if not current or current.get("resolved_at"):
        return False
    resolved = {**current, "resolved_at": _utc_now(), "resolved_by": reason}
    record["action_required"] = resolved
    _write_action_required_status(_status_file_for(record), resolved)
    return True


TERMINAL_SUMMARIES = {
    "completed_unverified": "Claude reported a successful result and the process exited cleanly. Repository result is NOT yet verified.",
    "failed_terminal": "Claude terminated without a successful, cleanly-exited result.",
    "incomplete_recoverable": "Claude stopped with work still outstanding (background workers killed or no changes produced).",
    "unknown_needs_reconciliation": "Claude's terminal state could not be established from the managed process; reconcile the repository manually.",
    "closed_by_user": "The coordinator closed the session and the managed process has ended — terminated by the close itself if it did not exit on its own. Whatever Claude had done is unverified; nothing here says the work finished.",
}


def _terminal_report(
    *,
    outcome: str,
    process_status: dict[str, Any],
    result: dict[str, Any],
    output: str,
) -> dict[str, Any]:
    """Compact, secret-safe description of why a job ended the way it did."""
    report: dict[str, Any] = {
        "outcome": outcome,
        "summary": TERMINAL_SUMMARIES.get(outcome, outcome),
        "exit_code": process_status.get("exit_code"),
        "completion_reason": process_status.get("completion_reason"),
        "termination_source": process_status.get("termination_source") or None,
        "result_subtype": result.get("subtype") if result else None,
        "result_event_seen": bool(result),
        "recorded_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
    if not result:
        report["no_result_event"] = (
            "The stream-json transcript contained no `\"type\":\"result\"` event, so "
            "Claude never reported a turn outcome. Treat repository state as unverified."
        )
        evidence = _terminal_evidence(output)
        if evidence:
            report["evidence"] = evidence
    return report


def _append_terminal_status(path: Path, report: dict[str, Any]) -> None:
    """Append one Hermes-authored terminal block without clobbering the agent's status."""
    marker = "## Terminal outcome (Hermes supervisor)"
    try:
        existing = path.read_text(encoding="utf-8")
    except OSError:
        existing = ""
    if marker in existing:
        return
    lines = [
        "",
        marker,
        f"- **Outcome:** {report.get('outcome')}",
        f"- **Summary:** {report.get('summary')}",
        f"- **Exit code:** {report.get('exit_code')}",
        f"- **Completion reason:** {report.get('completion_reason')}",
        f"- **Claude result event:** {'yes' if report.get('result_event_seen') else 'NO'}"
        + (f" (subtype: {report.get('result_subtype')})" if report.get("result_subtype") else ""),
        f"- **Recorded (UTC):** {report.get('recorded_at')}",
    ]
    if report.get("no_result_event"):
        lines.append(f"- **No-result failure:** {report['no_result_event']}")
    blocked = report.get("action_required")
    if isinstance(blocked, dict) and blocked.get("summary"):
        lines.append(f"- **Action required when it ended:** {blocked['summary']}")
        lines.append("- Hermes never granted that approval and never auto-approved it.")
    if report.get("evidence"):
        lines.append("- **Redacted tail:**")
        lines.append("```")
        lines.append(str(report["evidence"]))
        lines.append("```")
    block = "\n".join(lines) + "\n"
    # Trim the agent-authored part, never the terminal block: this is the only
    # record of *why* the job ended.
    head = existing.rstrip()[:MAX_STATUS_CHARS]
    content = (head + "\n" + block) if head else block.lstrip("\n")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(path.suffix + ".tmp")
        temporary.write_text(content, encoding="utf-8")
        temporary.replace(path)
    except OSError:
        pass


def _register_completion_watcher(process_session_id: str, launch: dict[str, Any]) -> dict[str, Any]:
    """Guarantee exactly one terminal notification for a watch-pattern job.

    `terminal_tool` only registers a gateway completion watcher when
    `notify_on_complete=True`, and it *drops* `watch_patterns` when both flags
    are passed together. A result-event watch therefore buys per-turn signals
    at the cost of every terminal signal: a launch failure, a crash, a
    no-result exit, or a normal close after `claude_code_close` all end
    silently, and `_move_to_finished` never enqueues a completion event either.

    Arming the completion side directly on the managed session restores both:
    the result-event watch keeps delivering live turns, and the watcher task
    started by the gateway after this turn delivers exactly one exit/failure
    notification into the originating conversation.
    """
    detail: dict[str, Any] = {
        "registered": False,
        "gateway_route": False,
        "already_exited": False,
        "reason": "",
    }
    if launch.get("notify_unsupported"):
        detail["reason"] = "async delivery is unsupported in this session; poll with claude_code_status"
        return detail
    try:
        from tools.process_registry import process_registry
    except Exception as exc:  # pragma: no cover - registry always present in Hermes
        detail["reason"] = f"process registry unavailable: {exc.__class__.__name__}"
        return detail
    session = process_registry.get(process_session_id)
    if session is None:
        detail["reason"] = "managed process session not found immediately after launch"
        return detail
    session.notify_on_complete = True
    if not session.watcher_interval:
        session.watcher_interval = COMPLETION_WATCH_INTERVAL_SECONDS
    already_watched = any(
        watcher.get("session_id") == session.id
        for watcher in process_registry.pending_watchers
    )
    if not already_watched:
        process_registry.pending_watchers.append({
            "session_id": session.id,
            "check_interval": session.watcher_interval,
            "session_key": session.session_key,
            "platform": session.watcher_platform,
            "chat_id": session.watcher_chat_id,
            "user_id": session.watcher_user_id,
            "user_name": session.watcher_user_name,
            "thread_id": session.watcher_thread_id,
            "message_id": session.watcher_message_id,
            "notify_on_complete": True,
            "parent_session_id": session.parent_session_id,
        })
    # Re-persist the checkpoint so a gateway restart re-arms this watcher from
    # ~/.hermes/processes.json instead of recovering a silent detached process.
    try:
        process_registry._write_checkpoint()
    except Exception:
        pass
    detail["registered"] = True
    detail["gateway_route"] = bool(session.watcher_platform)
    detail["already_exited"] = bool(session.exited)
    detail["watcher_interval"] = session.watcher_interval
    return detail


def _command(model: str, effort: str, max_turns: int, permission_mode: str, status_dir: Path) -> list[str]:
    command = [
        CLAUDE, "-p",
        "--add-dir", str(status_dir),
        "--input-format", "stream-json",
        "--output-format", "stream-json",
        "--verbose",
        "--replay-user-messages",
        "--model", model,
        "--effort", effort,
        "--max-turns", str(max_turns),
        "--permission-mode", permission_mode,
    ]
    # Claude's default print-mode background-agent wait ceiling is 10 minutes.
    # Hermes owns durable supervision, so Fable parents must not system-kill
    # their internal workers simply because that local ceiling elapsed.
    if model == "fable":
        return ["env", "CLAUDE_CODE_PRINT_BG_WAIT_CEILING_MS=0", *command]
    return command


def handle_dispatch(args: dict[str, Any], **_kw: Any) -> str:
    """Launch a new parent job. Lineage-bearing launches go through _dispatch."""
    return _dispatch(args, _kw)


def _dispatch(
    args: dict[str, Any], kw: dict[str, Any], *, restart_of_job_id: str = ""
) -> str:
    validated = _validate_args(args)
    if isinstance(validated, str):
        return _json({"success": False, "error": validated})
    task, workdir, model, effort, max_turns, permission_mode, parallel_lane = validated
    recovery_of_job_id = str(args.get("recovery_of_job_id") or "").strip()
    if recovery_of_job_id and any(c not in "abcdefghijklmnopqrstuvwxyz0123456789-" for c in recovery_of_job_id):
        return _json({"success": False, "error": "recovery_of_job_id is invalid"})
    if parallel_lane and recovery_of_job_id:
        return _json({"success": False, "error": (
            "parallel_lane is not available for a recovery job: a recovery is one isolated "
            "parent, never a same-turn fan-out. Drop parallel_lane."
        )})
    origin_task_id = str(kw.get("task_id") or "").strip()
    repo_id = _repo_identity(workdir)
    blocked = _dispatch_guard(
        origin_task_id=origin_task_id,
        repo_id=repo_id,
        recovery_of_job_id=recovery_of_job_id,
        parallel_lane=parallel_lane,
        workdir=workdir,
        restart_of_job_id=restart_of_job_id,
    )
    if blocked:
        return _json({"success": False, "error": blocked})
    if not Path(CLAUDE).is_file() and shutil.which("claude") is None:
        return _json({"success": False, "error": "Claude Code CLI is not installed or not on PATH"})
    job_id = f"claude-{uuid.uuid4().hex[:12]}"
    status_file = _status_path(job_id)
    inbox_file = _inbox_path(job_id)
    supervisor_protocol = f"""

## Required supervisor status protocol
You are supervised without exposing a full git diff. Immediately update this file and refresh it before/after each meaningful phase:
`{status_file}`

Keep it under {MAX_STATUS_CHARS} characters. It must contain only: current phase/objective, filenames or components being changed (no diff), latest validation command/result, blockers, and next action. Do not write prose transcripts, full diffs, or secrets there.

Follow-up requirements are delivered live and recorded for audit in:
`{inbox_file}`
Read the latest entry when you receive a follow-up, then update the status file before acting on it.
""".strip()
    initial_task = f"{task}\n\n{supervisor_protocol}"
    command = _command(model, effort, max_turns, permission_mode, RUNS_DIR)
    stream_command = (
        f"{{ printf '%s\\n' {shlex.quote(_stream_user_message(initial_task))}; cat; }} "
        f"| {shlex.join(command)}"
    )
    if bool(args.get("dry_run")):
        return _json({
            "success": True,
            "dry_run": True,
            "job_id": job_id,
            "status_file": str(status_file),
            "inbox_file": str(inbox_file),
            "command": stream_command,
            "workdir": str(workdir),
            "parallel_lane": parallel_lane or None,
            "restart_of_job_id": restart_of_job_id or None,
            **_loaded_code(),
        })

    _write_status_file(
        status_file,
        state="starting",
        workdir=workdir,
        model=model,
        detail="Waiting for Claude to begin. Claude is required to replace this with compact phase, validation, blocker, and next-action status.",
    )
    _append_inbox(inbox_file, task, label="initial task")

    # Use Hermes's tracked process registry. Stream-json keeps the Claude
    # session alive, while a result-event watch injects a callback after each
    # completed Claude turn into the originating gateway conversation.
    from tools.terminal_tool import terminal_tool
    launch = json.loads(terminal_tool(
        command=stream_command,
        background=True,
        watch_patterns=['"type":"result"'],
        pty=False,
        keep_stdin_open=True,
        workdir=str(workdir),
        task_id=str(kw.get("task_id") or "") or None,
        session_id=str(kw.get("session_id") or "") or None,
    ))
    process_session_id = str(launch.get("session_id") or "").strip()
    if not process_session_id:
        return _json({
            "success": False,
            "error": str(launch.get("error") or "Claude Code launch did not return a managed process session"),
            "launch": launch,
        })

    # A result-event watch alone has no terminal path (see
    # _register_completion_watcher); arm the completion side before the record
    # is written so its truthfulness is what gets persisted.
    watcher = _register_completion_watcher(process_session_id, launch)

    record: dict[str, Any] = {
        "job_id": job_id,
        "state": "running",
        "process_session_id": process_session_id,
        "pid": launch.get("pid"),
        "started_at": time.time(),
        "origin_task_id": origin_task_id or None,
        "repo_id": repo_id,
        "recovery_of_job_id": recovery_of_job_id or None,
        "restart_of_job_id": restart_of_job_id or None,
        "parallel_lane": parallel_lane or None,
        "workdir": str(workdir),
        "model": model,
        "effort": effort,
        "max_turns": max_turns,
        "permission_mode": permission_mode,
        "command": command,
        "status_file": str(status_file),
        "inbox_file": str(inbox_file),
        "streaming": True,
        "turn_callback": not bool(launch.get("notify_unsupported")),
        "notify_on_complete": bool(watcher.get("registered")),
        "completion_callback": bool(watcher.get("registered")),
        "completion_watcher": watcher,
        "notify_unsupported": launch.get("notify_unsupported"),
        **_loaded_code(),
    }
    if watcher.get("already_exited"):
        # The managed process died before this call returned (bad CLI, bad
        # workdir, immediate auth failure). Resolve it here rather than
        # handing back a job id that will never produce anything.
        process_status, output = _live_process_view(process_session_id)
        result = _last_stream_result(output)
        outcome = _classify_terminal_outcome(
            output,
            result,
            exit_code=process_status.get("exit_code"),
            completion_reason=str(process_status.get("completion_reason") or "exited"),
            observed=True,
        )
        _finalize_record(
            record, outcome, process_status=process_status, result=result, output=output
        )
        _write_record(record)
        return _json({
            "success": outcome == "completed_unverified",
            "job_id": job_id,
            "state": record["state"],
            "process_session_id": process_session_id,
            "model": model,
            "workdir": str(workdir),
            "parallel_lane": parallel_lane or None,
            "restart_of_job_id": restart_of_job_id or None,
            "status_file": str(status_file),
            "terminal_report": record.get("terminal_report"),
            "requires_explicit_recovery": bool(record.get("requires_explicit_recovery")),
            **_loaded_code(),
            "error": None if outcome == "completed_unverified" else (
                f"Claude exited immediately ({outcome}). Report this terminal state; do not auto-retry or fan out."
            ),
        })
    _write_record(record)
    # The record exists on disk before the watcher starts, so its first read
    # cannot miss the job it is supposed to be watching.
    _arm_record_reconciler(job_id, process_session_id)
    if record["completion_callback"]:
        callback_note = (
            "Hermes will notify this conversation when Claude completes each turn AND exactly once when the "
            "job terminates (success, crash, kill, or no-result failure)."
        )
    else:
        callback_note = (
            f"Terminal notification could not be armed ({watcher.get('reason') or 'unknown reason'}). "
            "Poll claude_code_status; do not assume silence means success."
        )
    return _json({
        "success": True,
        "job_id": job_id,
        "state": "running",
        "process_session_id": process_session_id,
        "pid": launch.get("pid"),
        "model": model,
        "workdir": str(workdir),
        "parallel_lane": parallel_lane or None,
        "restart_of_job_id": restart_of_job_id or None,
        "status_file": str(status_file),
        "inbox_file": str(inbox_file),
        "turn_callback": (
            "Hermes will notify this conversation when Claude completes each turn; keep the session alive for follow-up updates."
            if record["turn_callback"] else
            "Async turn delivery is unavailable in this session; use claude_code_status and claude_code_message manually."
        ),
        "completion_callback": callback_note,
        "next_step": "Continue other work. If requirements change while Claude is active, call claude_code_message with the same job_id; do not stop and redispatch.",
        **_loaded_code(),
    })


def handle_status(args: dict[str, Any], **_kw: Any) -> str:
    job_id = str(args.get("job_id") or "").strip()
    record = _read_record(job_id)
    if record is None:
        return _json({"success": False, "error": "unknown job_id"})

    process_session_id = str(record.get("process_session_id") or "").strip()
    view = _live_process_view(process_session_id) if process_session_id else ({}, "")
    process_status = view[0]
    _reconcile_record(record, view)

    _persist_record(record)
    if record.get("state") not in TERMINAL_STATES:
        _arm_record_reconciler(job_id, process_session_id)
    supervisor_status = _read_status_file(record)
    process_summary = {
        key: process_status.get(key)
        for key in ("session_id", "status", "pid", "uptime_seconds", "exit_code", "error")
        if key in process_status
    }
    state = str(record.get("state") or "unknown")
    action_detail = record.get("action_required")
    action_detail = action_detail if isinstance(action_detail, dict) else None
    return _json({
        "success": True,
        "job_id": job_id,
        "state": state,
        "terminal": state in TERMINAL_STATES,
        "action_required": state == "action_required",
        "action_required_detail": action_detail,
        "model": record.get("model"),
        "workdir": record.get("workdir"),
        "parallel_lane": record.get("parallel_lane") or None,
        "restart_of_job_id": record.get("restart_of_job_id") or None,
        "restarted_by_job_id": record.get("restarted_by_job_id") or None,
        "restart_action": _restart_action(record),
        "process_session_id": process_session_id or None,
        "process": process_summary or None,
        "turn_callback": bool(record.get("turn_callback")),
        "completion_callback": bool(record.get("completion_callback", record.get("notify_on_complete"))),
        "completion_signal": record.get("completion_signal"),
        "last_turn_result": record.get("last_turn_result"),
        "claude_result": record.get("claude_result"),
        "terminal_report": record.get("terminal_report"),
        "requires_explicit_recovery": bool(record.get("requires_explicit_recovery")),
        "recovery_guidance": record.get("recovery_guidance"),
        "supervisor_status": supervisor_status,
        **_loaded_code(),
    })


def _active_session_rows() -> list[dict[str, Any]]:
    """Every still-live job as one compact, secret-free row, deterministically ordered.

    Each live record is reconciled through the *passive* view first, so a job
    that died unobserved is reported as finished (and omitted) instead of being
    listed as running forever — without `poll()`/`wait()`/`read_log()`, any of
    which would make the core believe the user had already been told about the
    exit and drop its single completion notification.
    """
    rows: list[tuple[float, str, dict[str, Any]]] = []
    for record in _all_records():
        job_id = str(record.get("job_id") or "")
        if not job_id or record.get("state") in TERMINAL_STATES:
            continue
        process_session_id = str(record.get("process_session_id") or "").strip()
        reconciled = _reconcile_persisted_record(
            job_id, _passive_process_view(process_session_id)
        )
        if reconciled is not None:
            record = reconciled
        if record.get("state") in TERMINAL_STATES:
            continue
        # Still live: make sure something is watching for its exit, exactly as
        # the status/close paths do.
        _arm_record_reconciler(job_id, process_session_id)
        started_at = float(record.get("started_at") or 0)
        rows.append((started_at, job_id, {
            "job_id": job_id,
            "state": str(record.get("state") or "unknown"),
            "model": record.get("model"),
            "workdir": record.get("workdir"),
            "parallel_lane": record.get("parallel_lane") or None,
            "started_at": (
                time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(started_at))
                if started_at else None
            ),
            "action_required": record.get("state") == "action_required",
            "restart_pending": _restart_action(record) == "restart_pending_source_exit",
        }))
    rows.sort(key=lambda row: (row[0], row[1]))
    return [row[2] for row in rows]


def handle_list(_args: dict[str, Any] | None = None, **_kw: Any) -> str:
    """Compact inventory of live Claude sessions: no transcripts, no secrets."""
    sessions = _active_session_rows()
    return _json({
        "success": True,
        "count": len(sessions),
        "sessions": sessions,
        "next_step": (
            "Use claude_code_status with a job_id for that job's supervisor status; a row with "
            "action_required=true is blocked on Karthik's decision, not failed."
            if sessions else
            "No Claude session is active. Finished jobs are intentionally not listed."
        ),
        **_loaded_code(),
    })


def handle_message(args: dict[str, Any], **_kw: Any) -> str:
    """Queue a new user turn into a live stream-json Claude process."""
    job_id = str(args.get("job_id") or "").strip()
    message = str(args.get("message") or "").strip()
    if not message:
        return _json({"success": False, "error": "message is required"})
    if len(message) > MAX_TASK_CHARS:
        return _json({"success": False, "error": f"message exceeds {MAX_TASK_CHARS} characters"})
    record = _read_record(job_id)
    if record is None:
        return _json({"success": False, "error": "unknown job_id"})
    process_session_id = str(record.get("process_session_id") or "").strip()
    if not process_session_id:
        return _json({
            "success": False,
            "error": "This is a legacy one-shot Claude job and cannot receive live messages; start a new dispatch or resume its saved Claude session.",
        })

    from tools.process_registry import process_registry
    status = process_registry.poll(process_session_id)
    if status.get("status") != "running":
        # The job is over. Record the terminal state now so this failed
        # follow-up is itself a completion signal rather than a dead end.
        _reconcile_record(record)
        _persist_record(record)
        return _json({
            "success": False,
            "error": "Claude job is not running; do not redispatch automatically. Verify its repository result, then use a new isolated dispatch only if follow-up work is still needed.",
            "state": record.get("state"),
            "terminal_report": record.get("terminal_report"),
            "requires_explicit_recovery": bool(record.get("requires_explicit_recovery")),
            "process": status,
        })
    delivery = _submit_stream_event(
        process_registry, process_session_id, _stream_user_event_line(message)
    )
    if delivery.get("status") != "ok":
        return _json({
            "success": False,
            "error": "Could not queue the message in the running Claude session",
            "delivery": delivery,
        })
    _append_inbox(Path(str(record.get("inbox_file") or _inbox_path(job_id))), message, label="follow-up")
    record["messages_queued"] = int(record.get("messages_queued") or 0) + 1
    record["last_message_queued_at"] = time.time()
    previous_state = str(record.get("state") or "")
    # A real follow-up is the answer the blocked job was waiting for, so the
    # block is resolved and the transition is persisted for the supervisor.
    cleared = _resolve_action_required(record, "user_follow_up")
    if previous_state == "action_required":
        record["state"] = "running"
    _persist_record(record)
    _arm_record_reconciler(job_id, process_session_id)
    return _json({
        "success": True,
        "job_id": job_id,
        "process_session_id": process_session_id,
        "queued": True,
        "messages_queued": record["messages_queued"],
        "previous_state": previous_state,
        "state": record.get("state"),
        "cleared_action_required": cleared,
        "next_step": "Claude will process this as its next user turn after its current turn finishes. Do not stop or redispatch the job.",
        **_loaded_code(),
    })


def _await_session_exit(session: Any, grace_seconds: float) -> bool:
    """Wait out a bounded grace window for a managed child's own exit.

    Waits on the session's completion event — set by `_move_to_finished`, the
    same place the core enqueues the single user notification — so this observes
    the exit without `poll()`/`wait()`/`read_log()`, any of which would make the
    core believe that notification had already been delivered.
    """
    if session is None or getattr(session, "exited", False):
        return True
    event = getattr(session, "_completion_event", None)
    if event is not None:
        event.wait(max(grace_seconds, 0.0))
        return bool(getattr(session, "exited", False))
    deadline = time.monotonic() + max(grace_seconds, 0.0)
    while time.monotonic() < deadline:
        if getattr(session, "exited", False):
            return True
        time.sleep(0.05)
    return bool(getattr(session, "exited", False))


def _close_response(
    record: dict[str, Any], *, job_id: str, process_session_id: str, already_closed: bool
) -> str:
    state = str(record.get("state") or "closing")
    terminal = state in TERMINAL_STATES
    termination = record.get("close_termination")
    if terminal:
        next_step = (
            "The session is over and its terminal state is recorded above. Report the verified "
            "result. Hermes still delivers the single process-completion notification for it. "
            "To continue this work, dispatch fresh work or use claude_code_restart with this job id."
        )
    else:
        next_step = (
            "Termination was requested but could not be completed from here; the record stays "
            "`closing` and the exit watcher will finalize it. Do not treat this as a success."
        )
    return _json({
        "success": True,
        "job_id": job_id,
        "process_session_id": process_session_id,
        "state": state,
        "terminal": terminal,
        "already_closed": already_closed,
        "close_termination": termination,
        "terminal_report": record.get("terminal_report"),
        "completion_signal": record.get("completion_signal"),
        "next_step": next_step,
        **_loaded_code(),
    })


def handle_close(args: dict[str, Any], **_kw: Any) -> str:
    """End a live Claude session: EOF, a bounded grace, then a supported kill.

    Closing stdin alone is not a close. Claude's stdin is irrevocably shut the
    moment we send EOF, so a child that ignores it — or that is deep in a turn —
    is left running with no way to receive anything ever again (job
    claude-3c4901335c24). This asks for the clean shutdown first, then makes it
    true.
    """
    job_id = str(args.get("job_id") or "").strip()
    record = _read_record(job_id)
    if record is None:
        return _json({"success": False, "error": "unknown job_id"})
    process_session_id = str(record.get("process_session_id") or "").strip()
    if not process_session_id:
        return _json({"success": False, "error": "This legacy one-shot job has no live stream to close"})
    if record.get("state") in TERMINAL_STATES:
        # Idempotent: the session is already over. Never re-terminate, never
        # rewrite the sticky terminal block.
        return _close_response(
            record, job_id=job_id, process_session_id=process_session_id, already_closed=True
        )

    from tools.process_registry import process_registry

    termination: dict[str, Any] = {
        "requested_at": _utc_now(),
        "grace_seconds": CLOSE_GRACE_SECONDS,
        "stdin": "",
        "mode": "requested",
        "forced": False,
    }
    record["state"] = "closing"
    record["close_requested_at"] = record.get("close_requested_at") or time.time()
    record["closed_at"] = record.get("closed_at") or time.time()
    record["close_termination"] = termination

    delivery = process_registry.close_stdin(process_session_id)
    termination["stdin"] = str(delivery.get("status") or "")
    if delivery.get("status") not in {"ok", "already_closed", "already_exited", "not_found"}:
        # We could not even ask for the clean shutdown; say so rather than
        # reporting a close that did not happen.
        _persist_record(record)
        _arm_record_reconciler(job_id, process_session_id)
        return _json({
            "success": False,
            "error": "Could not close Claude session stdin",
            "delivery": delivery,
            "state": record.get("state"),
            **_loaded_code(),
        })
    _persist_record(record)

    session = process_registry.get(process_session_id)
    if not _await_session_exit(session, CLOSE_GRACE_SECONDS):
        # It ignored EOF. Escalate through the registry's own terminate/kill
        # path: it validates the PID's identity before signalling (so a recycled
        # PID can never be killed), tree-kills, and escalates SIGTERM to SIGKILL
        # after its own bounded grace. `consume_output=False` is mandatory —
        # the consuming variant would mark the completion delivered and the
        # user's single notification would be dropped.
        termination["mode"] = "forced"
        termination["forced"] = True
        termination["termination_source"] = CLOSE_TERMINATION_SOURCE
        record["close_forced_at"] = time.time()
        # Persisted before the kill so whichever writer finalizes the record —
        # this call or the exit watcher woken by it — agrees on why it ended.
        _persist_record(record)
        outcome = process_registry.kill_process(
            process_session_id, source=CLOSE_TERMINATION_SOURCE, consume_output=False
        )
        termination["kill_status"] = str(outcome.get("status") or "")
        if outcome.get("status") not in {"killed", "already_exited", "not_found"}:
            termination["error"] = _redact(str(outcome.get("error") or ""))[:200]
    else:
        termination["mode"] = "graceful"

    _reconcile_record(record, _passive_process_view(process_session_id))
    _persist_record(record)
    if record.get("state") not in TERMINAL_STATES:
        # Termination could not be completed here (a recovered session with no
        # runtime handle, or a registry error). Nothing else revisits a record
        # on its own, so leave the exit watcher armed.
        _arm_record_reconciler(job_id, process_session_id)
    return _close_response(
        record, job_id=job_id, process_session_id=process_session_id, already_closed=False
    )


def _restart_refusal(record: dict[str, Any], source_job_id: str) -> str | None:
    """Why this source may not be replaced, or None if exactly one may be."""
    state = str(record.get("state") or "")
    if state in LIVE_STATES and state != "closing":
        return (
            f"restart refused: {source_job_id} is still live ({state}) and can still receive work. "
            "Send the new requirements with claude_code_message on the same job id; restart only "
            "replaces a session whose stdin is already closed."
        )
    if not record.get("closed_at"):
        return (
            f"restart refused: {source_job_id} was never interrupted by claude_code_close, so there "
            "is no closed stream to replace. Dispatch the new work normally."
        )
    if str(record.get("model") or "") == "fable":
        return (
            f"restart refused: {source_job_id} is a Fable parent. Fable work stays one lineage under "
            "the recovery fence; do not restart, downgrade, or fan it out."
        )
    if state in RECOVERY_FENCED_STATES:
        return (
            f"restart refused: {source_job_id} ended in {state}. A failed parent is governed by the "
            "recovery fence, not by restart: report the verified failure and wait for an explicit "
            "user-directed recovery that names it."
        )
    if record.get("restart_of_job_id"):
        return (
            f"restart refused: {source_job_id} is already the replacement of "
            f"{record.get('restart_of_job_id')}. A restart lineage is exactly one replacement; "
            "start fresh work with claude_code_dispatch instead."
        )
    existing_child = str(record.get("restarted_by_job_id") or "")
    if not existing_child:
        for other in _all_records():
            if str(other.get("restart_of_job_id") or "") == source_job_id:
                existing_child = str(other.get("job_id") or "")
                break
    if existing_child:
        return (
            f"restart refused: {source_job_id} already has replacement {existing_child}. "
            "One closed parent permits exactly one replacement; send further requirements there."
        )
    return None


def handle_restart(args: dict[str, Any], **_kw: Any) -> str:
    """Start the one authorized replacement for a prematurely closed session."""
    source_job_id = str(args.get("source_job_id") or "").strip()
    task = str(args.get("task") or "").strip()
    if not task:
        return _json({"success": False, "restarted": False, "error": (
            "task is required: a restart must carry the current requirements, never a silent "
            "replay of the closed job's original task."
        )})
    if args.get("parallel_lane") is not None:
        return _json({"success": False, "restarted": False, "error": (
            "parallel_lane is not available for a restart: a replacement is one isolated parent, "
            "never a same-turn parallel variant."
        )})
    record = _read_record(source_job_id)
    if record is None:
        return _json({"success": False, "restarted": False, "error": "unknown source_job_id"})

    refusal = _restart_refusal(record, source_job_id)
    if refusal:
        return _json({"success": False, "restarted": False, "error": refusal})

    # Reconcile the source before deciding: it may have finished terminating
    # since the record was last written. Passive, so the core keeps its single
    # completion notification.
    process_session_id = str(record.get("process_session_id") or "").strip()
    record = _reconcile_persisted_record(
        source_job_id, _passive_process_view(process_session_id)
    ) or record
    origin_task_id = str(_kw.get("task_id") or "").strip()

    if record.get("state") not in TERMINAL_STATES:
        # Still winding down. Record the intent durably, start nothing, and say
        # exactly that — the source is never killed from here.
        previous = record.get("restart_pending")
        previous = previous if isinstance(previous, dict) else {}
        _annotate_record(source_job_id, {"restart_pending": {
            "requested_at": previous.get("requested_at") or _utc_now(),
            "requested_by_task_id": previous.get("requested_by_task_id") or (origin_task_id or None),
            "task_chars": len(task),
        }})
        return _json({
            "success": False,
            "restarted": False,
            "action": "restart_pending_source_exit",
            "job_id": None,
            "source_job_id": source_job_id,
            "source_state": str(record.get("state") or "unknown"),
            "error": (
                f"restart not started: {source_job_id} has not finished terminating "
                f"(state {record.get('state')}). The request is recorded, nothing was launched, and "
                "the source was not killed."
            ),
            "next_step": (
                f"Call claude_code_close on {source_job_id} to complete the termination it already "
                "began, or wait for its completion notification, then call claude_code_restart "
                "again with the same source_job_id and the current requirements."
            ),
            **_loaded_code(),
        })

    lineage = [f"This session replaces Claude job {source_job_id}. That session's stream was closed "
               "before the work was finished and its stdin cannot be reopened."]
    if record.get("status_file"):
        lineage.append(f"Its supervisor status file: {record['status_file']}")
    if record.get("inbox_file"):
        lineage.append(f"Its requirement history: {record['inbox_file']}")
    lineage.append("Read those for context. The requirements below are current and authoritative.")
    payload: dict[str, Any] = {
        "task": "\n".join(lineage) + f"\n\n{task}",
        "workdir": str(record.get("workdir") or ""),
        "model": record.get("model") or "opus",
    }
    for field in ("effort", "max_turns", "permission_mode"):
        if record.get(field):
            payload[field] = record[field]
    if record.get("recovery_of_job_id"):
        # An authorized recovery stays one chain: the replacement inherits the
        # lineage rather than minting a second recovery of the failed parent.
        payload["recovery_of_job_id"] = record["recovery_of_job_id"]

    answer = json.loads(_dispatch(payload, _kw, restart_of_job_id=source_job_id))
    answer["source_job_id"] = source_job_id
    answer["restarted"] = bool(answer.get("success") and answer.get("job_id"))
    if answer["restarted"]:
        _annotate_record(source_job_id, {
            "restarted_by_job_id": answer["job_id"],
            "restart_requested_at": time.time(),
            "restart_pending": None,
        })
    return _json(answer)


def _annotate_todo_result(
    *, tool_name: str = "", args: Any = None, result: Any = None, **_kw: Any
) -> str | None:
    """Add a routing checkpoint to the next model step after a task plan."""
    if tool_name != "todo" or not isinstance(args, dict) or not isinstance(args.get("todos"), list):
        return None
    if not isinstance(result, str):
        return None
    try:
        parsed = json.loads(result)
    except json.JSONDecodeError:
        return None
    summary = parsed.get("summary") if isinstance(parsed, dict) else None
    total = summary.get("total", 0) if isinstance(summary, dict) else 0
    if not isinstance(total, int) or total < 2:
        return None
    return f"{result}\n\n{TODO_ROUTING_CHECKPOINT}"


def register(ctx: Any) -> None:
    ctx.register_tool(
        name="claude_code_dispatch",
        toolset="delegation",
        schema=DISPATCH_SCHEMA,
        handler=handle_dispatch,
        check_fn=lambda: bool(shutil.which("claude") or Path(CLAUDE).is_file()),
        description="Dispatch heavy engineering work to Claude Code via the authenticated Claude Max CLI; Opus is the default and Fable is reserved for big work.",
        emoji="🧠",
    )
    ctx.register_tool(
        name="claude_code_message",
        toolset="delegation",
        schema=MESSAGE_SCHEMA,
        handler=handle_message,
        check_fn=lambda: bool(shutil.which("claude") or Path(CLAUDE).is_file()),
        description="Queue a requirement into an active Claude Code stream session without killing or redispatching it.",
        emoji="📨",
    )
    ctx.register_tool(
        name="claude_code_close",
        toolset="delegation",
        schema=CLOSE_SCHEMA,
        handler=handle_close,
        check_fn=lambda: True,
        description="End a live Claude session after its final verified turn: EOF, short grace, then terminate the managed process.",
        emoji="🔚",
    )
    ctx.register_tool(
        name="claude_code_restart",
        toolset="delegation",
        schema=RESTART_SCHEMA,
        handler=handle_restart,
        check_fn=lambda: bool(shutil.which("claude") or Path(CLAUDE).is_file()),
        description="Start the one authorized replacement for a Claude Code session that was closed before its work was finished.",
        emoji="♻️",
    )
    ctx.register_tool(
        name="claude_code_list",
        toolset="delegation",
        schema=LIST_SCHEMA,
        handler=handle_list,
        check_fn=lambda: True,
        description="List the still-active Claude Code sessions compactly, with lane and action-required flags.",
        emoji="🗂️",
    )
    ctx.register_tool(
        name="claude_code_status",
        toolset="delegation",
        schema=STATUS_SCHEMA,
        handler=handle_status,
        check_fn=lambda: True,
        description="Inspect a Claude Code job dispatched by this plugin.",
        emoji="📋",
    )

    def inject_routing_policy(**_kwargs: Any) -> dict[str, str]:
        return {"context": ROUTING_POLICY}

    ctx.register_hook("pre_llm_call", inject_routing_policy)
    ctx.register_hook("transform_tool_result", _annotate_todo_result)
