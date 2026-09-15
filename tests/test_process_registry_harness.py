"""Integration harness: the real Hermes process rail, real child processes.

The unit suite proves the plugin *registers* the completion callback. This
harness proves the registration is one the core actually honours: a fresh
``tools.process_registry.ProcessRegistry`` is driven through every terminal
path a delegated Claude job can take, and each one must produce exactly one
completion event plus a durable, correctly-classified job record.

Skipped (not failed) when the Hermes agent tree is not importable.
"""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

import pytest

from conftest import load_router, spawn_writable

HERMES_AGENT = Path(
    os.environ.get("HERMES_AGENT_DIR", Path.home() / ".hermes" / "hermes-agent")
)

pytestmark = pytest.mark.skipif(
    not (HERMES_AGENT / "tools" / "process_registry.py").is_file(),
    reason="Hermes agent tree not available",
)

RESULT_OK = '{"type":"result","subtype":"success","num_turns":3,"duration_ms":1200}'
WAIT_SECONDS = 20


@pytest.fixture
def registry_module(tmp_path, monkeypatch):
    """A real ProcessRegistry with its checkpoint redirected into tmp."""
    if str(HERMES_AGENT) not in sys.path:
        sys.path.insert(0, str(HERMES_AGENT))
    import tools.process_registry as pr

    monkeypatch.setattr(pr, "CHECKPOINT_PATH", tmp_path / "processes.json")
    registry = pr.ProcessRegistry()
    monkeypatch.setattr(pr, "process_registry", registry)
    yield pr, registry
    for session_id in list(registry._running):
        try:
            registry.kill_process(session_id, consume_output=False)
        except Exception:
            pass


def _await_exit(session) -> None:
    deadline = time.time() + WAIT_SECONDS
    while time.time() < deadline:
        if session.exited:
            return
        time.sleep(0.05)
    raise AssertionError(f"process {session.id} did not exit within {WAIT_SECONDS}s")


def _drain(registry) -> list[dict]:
    events = []
    while not registry.completion_queue.empty():
        events.append(registry.completion_queue.get_nowait())
    return events


def _run(router, registry, command: str, *, workdir: Path, kill_after: float = 0.0):
    """Spawn a real child, arm the plugin's completion callback, wait it out."""
    session = registry.spawn_local(command, cwd=str(workdir))
    watcher = router._register_completion_watcher(session.id, {})
    assert watcher["registered"] is True, watcher
    assert session.notify_on_complete is True
    if kill_after:
        time.sleep(kill_after)
        registry.kill_process(session.id, consume_output=False)
    _await_exit(session)
    time.sleep(0.3)  # let the reader thread flush its tail
    return session, watcher


def _record_for(router, session, registry, workdir: Path) -> dict:
    """Build and finalize a job record the way handle_status/dispatch do."""
    record = {
        "job_id": "claude-harness01",
        "state": "running",
        "process_session_id": session.id,
        "started_at": session.started_at,
        "model": "opus",
        "repo_id": str(workdir),
        "workdir": str(workdir),
        "status_file": str(router.RUNS_DIR / "claude-harness01.status.md"),
    }
    router._reconcile_record(record)
    router._write_record(record)
    return record


# --------------------------------------------------------------------------

TERMINAL_SCENARIOS = [
    pytest.param(
        f"printf '%s\\n' '{RESULT_OK}'; exit 0",
        "completed_unverified",
        0,
        id="successful-result-callback",
    ),
    pytest.param(
        "echo 'fatal: claude could not start' 1>&2; exit 9",
        "failed_terminal",
        9,
        id="terminal-process-failure-callback",
    ),
    pytest.param(
        "printf 'garbled }{ not json\\n'; exit 0",
        "unknown_needs_reconciliation",
        0,
        id="malformed-no-result-output",
    ),
    pytest.param(
        f"printf '%s\\n' '{RESULT_OK}'; printf 'session ended\\n'; sleep 0.2; exit 0",
        "completed_unverified",
        0,
        id="completed-result-then-process-exit",
    ),
]


@pytest.mark.parametrize("command,expected_state,expected_exit", TERMINAL_SCENARIOS)
def test_every_terminal_path_notifies_and_persists(
    registry_module, runs_dir, workdir, command, expected_state, expected_exit
):
    pr, registry = registry_module
    router = load_router(runs_dir)

    session, watcher = _run(router, registry, command, workdir=workdir)

    # 1. The core rail produced exactly one completion event for this session.
    completions = [
        evt for evt in _drain(registry)
        if evt.get("type") == "completion" and evt.get("session_id") == session.id
    ]
    assert len(completions) == 1
    assert completions[0]["exit_code"] == expected_exit
    assert completions[0]["completion_reason"] == "exited"

    # 2. The gateway-side watcher the plugin registered is present and armed.
    entries = [w for w in registry.pending_watchers if w["session_id"] == session.id]
    assert len(entries) == 1
    assert entries[0]["notify_on_complete"] is True
    assert entries[0]["check_interval"] == router.COMPLETION_WATCH_INTERVAL_SECONDS

    # 3. The job record reaches a durable, correctly-classified terminal state.
    record = _record_for(router, session, registry, workdir)
    assert record["state"] == expected_state
    assert record["completion_signal"]["outcome"] == expected_state
    reloaded = json.loads((runs_dir / "claude-harness01.json").read_text())
    assert reloaded["state"] == expected_state
    assert reloaded["terminal_report"]["exit_code"] == expected_exit
    status_text = Path(record["status_file"]).read_text()
    assert "Terminal outcome (Hermes supervisor)" in status_text
    if expected_state != "completed_unverified":
        assert reloaded["requires_explicit_recovery"] is True


def test_killed_job_is_a_failure_not_a_success(registry_module, runs_dir, workdir):
    pr, registry = registry_module
    router = load_router(runs_dir)

    session, _ = _run(
        registry=registry,
        router=router,
        command=f"printf '%s\\n' '{RESULT_OK}'; sleep 30",
        workdir=workdir,
        kill_after=0.6,
    )

    record = _record_for(router, session, registry, workdir)

    # A success result event was on the wire, but the process was killed.
    assert '"type":"result"' in session.output_buffer
    assert record["state"] == "failed_terminal"
    assert record["requires_explicit_recovery"] is True


def test_checkpoint_records_the_armed_completion_callback(
    registry_module, runs_dir, workdir, tmp_path
):
    """A gateway restart must be able to re-arm the watcher from disk."""
    pr, registry = registry_module
    router = load_router(runs_dir)

    session = registry.spawn_local("sleep 5", cwd=str(workdir))
    router._register_completion_watcher(session.id, {})

    entries = json.loads((tmp_path / "processes.json").read_text())
    mine = [e for e in entries if e["session_id"] == session.id]
    assert mine and mine[0]["notify_on_complete"] is True
    assert mine[0]["watcher_interval"] == router.COMPLETION_WATCH_INTERVAL_SECONDS

    registry.kill_process(session.id, consume_output=False)


def test_unsupported_async_delivery_is_not_silently_armed(registry_module, runs_dir):
    pr, registry = registry_module
    router = load_router(runs_dir)

    watcher = router._register_completion_watcher(
        "proc_does_not_exist", {"notify_unsupported": "one-shot runner"}
    )

    assert watcher["registered"] is False
    assert registry.pending_watchers == []


def test_queued_follow_ups_are_consumable_stream_json_lines(
    registry_module, runs_dir, workdir
):
    """End-to-end framing proof against the real rail and a real line reader.

    The child is the same shape as the Claude launch pipe (``printf`` the first
    event, then ``cat`` the live stdin) feeding a line-oriented consumer. Every
    queued follow-up must arrive as exactly one line; a missing delimiter
    swallows the event into the next read, and a doubled one emits an empty
    event the JSON parser rejects.
    """
    import shlex

    pr, registry = registry_module
    router = load_router(runs_dir)

    first = router._stream_user_message("first turn")
    command = (
        "{ printf '%s\\n' " + shlex.quote(first) + "; cat; } | "
        "while IFS= read -r line; do printf 'EVENT|%s\\n' \"$line\"; done"
    )
    session = spawn_writable(registry, command, cwd=str(workdir))
    try:
        for text in ("follow up one", "follow up two"):
            delivery = router._submit_stream_event(
                registry, session.id, router._stream_user_event_line(text)
            )
            assert delivery["status"] == "ok", delivery
        registry.close_stdin(session.id)
        _await_exit(session)
        time.sleep(0.3)
    finally:
        if not session.exited:
            registry.kill_process(session.id, consume_output=False)

    lines = [
        line for line in session.output_buffer.splitlines() if line.startswith("EVENT|")
    ]
    assert len(lines) == 3, session.output_buffer
    events = [json.loads(line.split("|", 1)[1]) for line in lines]
    assert [event["message"]["content"] for event in events] == [
        "first turn", "follow up one", "follow up two",
    ]
    assert all(event["type"] == "user" for event in events)
