"""The durable record must converge on its own when the process really exits.

`claude_code_close` asks for EOF and gives Claude a bounded grace to end its own
turn; when it takes it, the exit — not the close — is what has to be recorded.
Hermes emits exactly one completion notification into the originating
conversation, but a notification is a *message*, not a record write. Job
`claude-ce67a6761716` proved the gap: one clean completion callback, and the
durable record still read

    state='closing' outcome=None completed_at=None
    completion_signal=None terminal_report=None

until somebody manually polled `claude_code_status`. Nothing in this file is
allowed to call that tool: convergence has to happen because the process ended.
"""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

import pytest

from conftest import (
    FakeRegistry, FakeSession, exit_during_grace, install_fake_rail, load_router,
)

RESULT_OK = '{"type":"result","subtype":"success","num_turns":4,"duration_ms":900}\n'
LAUNCH_OK = {"session_id": "proc_fake000000", "pid": 4242}

TERMINAL_FIELDS = ("state", "outcome", "completed_at", "completion_signal", "terminal_report")


def _dispatch(router, workdir: Path, registry: FakeRegistry, monkeypatch):
    install_fake_rail(monkeypatch, registry, LAUNCH_OK)
    return json.loads(router.handle_dispatch(
        {"task": "do the work", "workdir": str(workdir)},
        task_id="turn-1",
        session_id="sess-parent",
    ))


def _ban_status_polls(router, monkeypatch):
    """Make `claude_code_status` fatal, so only real convergence can pass."""
    def forbidden(*_a, **_kw):
        raise AssertionError("claude_code_status must not be needed to reach a terminal state")
    monkeypatch.setattr(router, "handle_status", forbidden)


def _await_terminal(router, job_id: str, timeout: float = 15.0) -> dict:
    """Wait for the on-disk record to go terminal by itself."""
    worker = getattr(router, "_RECONCILERS", {}).get(job_id)
    if worker is not None:
        worker.join(timeout)
    deadline = time.monotonic() + timeout
    record = router._read_record(job_id) or {}
    while time.monotonic() < deadline:
        if record.get("state") in router.TERMINAL_STATES:
            return record
        time.sleep(0.02)
        record = router._read_record(job_id) or {}
    return record


def _assert_atomically_terminal(record: dict, expected_state: str) -> None:
    missing = [field for field in TERMINAL_FIELDS if not record.get(field)]
    assert not missing, f"terminal record is missing {missing}: {record.get('state')!r}"
    assert record["state"] == expected_state
    assert record["completion_signal"]["outcome"] == expected_state
    assert record["terminal_report"]["outcome"] == expected_state
    assert isinstance(record["completed_at"], (int, float))


# --------------------------------------------------------------------------
# 1. The exact reported regression, on the fake rail
# --------------------------------------------------------------------------

def test_process_exit_after_close_converges_without_a_status_poll(
    router, workdir, monkeypatch
):
    session = FakeSession()
    registry = FakeRegistry(session)
    response = _dispatch(router, workdir, registry, monkeypatch)
    job_id = response["job_id"]
    _ban_status_polls(router, monkeypatch)

    # Claude finishes its final turn inside the close grace and the managed
    # process really ends, exactly as _move_to_finished does it.
    exit_during_grace(session, exit_code=0, output=RESULT_OK)
    closed = json.loads(router.handle_close({"job_id": job_id}))
    assert closed["state"] == "completed_unverified" and closed["terminal"] is True

    record = _await_terminal(router, job_id)
    _assert_atomically_terminal(record, "completed_unverified")
    assert record["completion_callback"] is True
    assert record["terminal_report"]["exit_code"] == 0
    assert record["terminal_report"]["result_event_seen"] is True
    assert not record.get("requires_explicit_recovery")


@pytest.mark.parametrize("exit_code,output,expected", [
    (0, RESULT_OK, "completed_unverified"),
    (9, "fatal: claude crashed\n", "failed_terminal"),
    (0, "chatter with no result event\n", "unknown_needs_reconciliation"),
    (0, "background tasks still running after the run\n", "incomplete_recoverable"),
])
def test_every_exit_shape_classifies_itself_on_exit(
    router, workdir, monkeypatch, exit_code, output, expected
):
    session = FakeSession()
    registry = FakeRegistry(session)
    response = _dispatch(router, workdir, registry, monkeypatch)
    job_id = response["job_id"]
    _ban_status_polls(router, monkeypatch)

    exit_during_grace(session, exit_code=exit_code, output=output)
    router.handle_close({"job_id": job_id})

    record = _await_terminal(router, job_id)
    _assert_atomically_terminal(record, expected)
    assert bool(record.get("requires_explicit_recovery")) is (
        expected in router.RECOVERY_FENCED_STATES
    )


def test_exit_without_a_close_also_converges(router, workdir, monkeypatch):
    """A crash mid-run must not wait for the coordinator to close anything."""
    session = FakeSession()
    registry = FakeRegistry(session)
    response = _dispatch(router, workdir, registry, monkeypatch)
    job_id = response["job_id"]
    _ban_status_polls(router, monkeypatch)

    session.finish(exit_code=1, reason="killed", output="")

    record = _await_terminal(router, job_id)
    _assert_atomically_terminal(record, "failed_terminal")
    assert record["requires_explicit_recovery"] is True


# --------------------------------------------------------------------------
# 2. Exactly-once, and no notification-suppressing side effects
# --------------------------------------------------------------------------

def test_reconciliation_never_consumes_the_user_callback(
    router, workdir, monkeypatch
):
    """`wait()`/`read_log()`/`poll()` all suppress the one user notification.

    `_completion_consumed` (wait/read_log) makes the gateway and TUI watchers
    skip delivery outright; `_poll_observed` (poll) makes the CLI drain skip
    it. Background reconciliation therefore has to read the session object
    directly and must not touch any of those three entry points.
    """
    session = FakeSession()
    registry = FakeRegistry(session)
    response = _dispatch(router, workdir, registry, monkeypatch)
    job_id = response["job_id"]
    _ban_status_polls(router, monkeypatch)

    polls_before_close = registry.polls
    exit_during_grace(session, exit_code=0, output=RESULT_OK)
    router.handle_close({"job_id": job_id})

    record = _await_terminal(router, job_id)

    assert record["state"] == "completed_unverified"
    assert registry.polls == polls_before_close, "background reconciliation must not poll()"
    assert registry.stdin_writes == [], "reconciliation must not talk to Claude"
    assert registry.is_completion_consumed("proc_fake000000") is False


def test_terminal_block_is_written_exactly_once(router, workdir, monkeypatch):
    session = FakeSession()
    registry = FakeRegistry(session)
    response = _dispatch(router, workdir, registry, monkeypatch)
    job_id = response["job_id"]

    exit_during_grace(session, exit_code=0, output=RESULT_OK)
    router.handle_close({"job_id": job_id})
    record = _await_terminal(router, job_id)
    assert record["state"] == "completed_unverified"

    recorded_at = record["completion_signal"]["recorded_at"]
    # A later status poll is allowed; it just must not duplicate or rewrite.
    again = json.loads(router.handle_status({"job_id": job_id}))
    assert again["state"] == "completed_unverified"
    assert again["completion_signal"]["recorded_at"] == recorded_at

    status_text = Path(record["status_file"]).read_text(encoding="utf-8")
    assert status_text.count("## Terminal outcome (Hermes supervisor)") == 1


def test_reconciliation_preserves_a_pending_approval_block(
    router, workdir, monkeypatch
):
    """A job that died still awaiting a human decision must say so."""
    session = FakeSession()
    registry = FakeRegistry(session)
    response = _dispatch(router, workdir, registry, monkeypatch)
    job_id = response["job_id"]
    _ban_status_polls(router, monkeypatch)

    record = router._read_record(job_id)
    record["action_required"] = {
        "signal": "permission_request",
        "confidence": "high",
        "tools": ["Bash"],
        "summary": "Claude needs approval to run the deploy script.",
        "evidence": "requested permissions to use Bash",
        "detected_at": router._utc_now(),
    }
    record["state"] = "action_required"
    router._write_record(record)

    session.finish(exit_code=0, output="no result event\n")

    settled = _await_terminal(router, job_id)
    _assert_atomically_terminal(settled, "unknown_needs_reconciliation")
    blocked = settled["terminal_report"]["action_required"]
    assert blocked["summary"] == "Claude needs approval to run the deploy script."
    assert "never granted" in settled["terminal_report"]["summary"]


def test_a_terminal_record_is_never_rewritten_by_a_late_exit(
    router, workdir, monkeypatch
):
    session = FakeSession()
    registry = FakeRegistry(session)
    response = _dispatch(router, workdir, registry, monkeypatch)
    job_id = response["job_id"]

    session.finish(exit_code=9, output="fatal\n")
    first = _await_terminal(router, job_id)
    assert first["state"] == "failed_terminal"
    stamp = first["completion_signal"]["recorded_at"]

    # Re-arming after the fact must be a no-op on a sticky terminal record.
    router._arm_record_reconciler(job_id, "proc_fake000000")
    settled = _await_terminal(router, job_id)
    assert settled["state"] == "failed_terminal"
    assert settled["completion_signal"]["recorded_at"] == stamp


# --------------------------------------------------------------------------
# 3. The same proof against the real Hermes rail and a real child process
# --------------------------------------------------------------------------

HERMES_AGENT = Path(
    os.environ.get("HERMES_AGENT_DIR", Path.home() / ".hermes" / "hermes-agent")
)


@pytest.fixture
def real_registry(tmp_path, monkeypatch):
    if not (HERMES_AGENT / "tools" / "process_registry.py").is_file():
        pytest.skip("Hermes agent tree not available")
    if str(HERMES_AGENT) not in sys.path:
        sys.path.insert(0, str(HERMES_AGENT))
    import tools.process_registry as pr

    monkeypatch.setattr(pr, "CHECKPOINT_PATH", tmp_path / "processes.json")
    registry = pr.ProcessRegistry()
    monkeypatch.setattr(pr, "process_registry", registry)
    yield registry
    for session_id in list(registry._running):
        try:
            registry.kill_process(session_id, consume_output=False)
        except Exception:
            pass


def test_real_managed_process_exit_after_close_converges_by_itself(
    real_registry, runs_dir, workdir, monkeypatch
):
    """End-to-end on the real rail: close -> EOF -> real exit -> terminal record.

    The child mirrors the Claude launch pipe: it consumes stdin until EOF,
    keeps working briefly (so `claude_code_close` genuinely observes a running
    process and writes `closing`), then emits its final result event and exits.
    """
    router = load_router(runs_dir)
    _ban_status_polls(router, monkeypatch)

    result_line = RESULT_OK.strip()
    session = real_registry.spawn_local(
        f"cat > /dev/null; sleep 1; printf '%s\\n' '{result_line}'; exit 0",
        cwd=str(workdir),
        keep_stdin_open=True,
    )
    watcher = router._register_completion_watcher(session.id, {})
    assert watcher["registered"] is True, watcher

    job_id = "claude-realexit01"
    router._write_record({
        "job_id": job_id,
        "state": "running",
        "process_session_id": session.id,
        "started_at": session.started_at,
        "model": "opus",
        "repo_id": str(workdir),
        "workdir": str(workdir),
        "completion_callback": True,
        "status_file": str(runs_dir / f"{job_id}.status.md"),
    })

    closed = json.loads(router.handle_close({"job_id": job_id}))
    assert closed["state"] == "completed_unverified", closed
    assert closed["close_termination"]["forced"] is False

    record = _await_terminal(router, job_id, timeout=30.0)
    _assert_atomically_terminal(record, "completed_unverified")
    assert record["terminal_report"]["exit_code"] == 0
    assert record["terminal_report"]["result_event_seen"] is True

    # The core still owns the single user-facing notification, and nothing the
    # plugin did marked it consumed or observed.
    completions = []
    while not real_registry.completion_queue.empty():
        event = real_registry.completion_queue.get_nowait()
        if event.get("type") == "completion" and event.get("session_id") == session.id:
            completions.append(event)
    assert len(completions) == 1, completions
    assert real_registry.is_completion_consumed(session.id) is False
    assert session.id not in real_registry._poll_observed

    status_text = Path(record["status_file"]).read_text(encoding="utf-8")
    assert status_text.count("## Terminal outcome (Hermes supervisor)") == 1
