"""`claude_code_close` must end the session, not just shut its stdin.

A coordinator that closes a stream is done with the job. The old contract
closed stdin and hoped: a child that never reads stdin, or that is mid-turn,
kept running with no writable input at all — job `claude-3c4901335c24` hung
exactly that way. Close now asks for EOF, waits a short bounded grace for the
child's own exit, and otherwise escalates through the ProcessRegistry's
supported terminate/kill API (never a raw PID signal), recording which of the
two happened. `closing` is transitional only.
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
FAST_GRACE = 0.15

HERMES_AGENT = Path(
    os.environ.get("HERMES_AGENT_DIR", Path.home() / ".hermes" / "hermes-agent")
)


def _ban_status_polls(router, monkeypatch):
    def forbidden(*_a, **_kw):
        raise AssertionError("close must terminalize without a claude_code_status poll")
    monkeypatch.setattr(router, "handle_status", forbidden)


def _dispatch(router, workdir: Path, registry: FakeRegistry, monkeypatch) -> dict:
    install_fake_rail(monkeypatch, registry, LAUNCH_OK)
    return json.loads(router.handle_dispatch(
        {"task": "do the work", "workdir": str(workdir)},
        task_id="turn-1", session_id="sess-parent",
    ))


def _close(router, job_id: str) -> dict:
    return json.loads(router.handle_close({"job_id": job_id}))


# --------------------------------------------------------------------------
# 1. A child that ignores stdin is actually terminated
# --------------------------------------------------------------------------

def test_close_terminates_a_child_that_ignores_stdin(router, workdir, monkeypatch):
    session = FakeSession()
    registry = FakeRegistry(session)
    response = _dispatch(router, workdir, registry, monkeypatch)
    job_id = response["job_id"]
    _ban_status_polls(router, monkeypatch)
    router.CLOSE_GRACE_SECONDS = FAST_GRACE

    closed = _close(router, job_id)

    assert session.exited is True, "close left a live child behind"
    assert closed["terminal"] is True
    assert closed["state"] == "closed_by_user"
    assert registry.kills and registry.kills[0]["session_id"] == session.id
    # Never a raw signal, and never the consuming variant.
    assert registry.kills[0]["consume_output"] is False
    assert registry.kills[0]["source"] == router.CLOSE_TERMINATION_SOURCE


def test_closing_never_survives_the_close_call(router, workdir, monkeypatch):
    session = FakeSession()
    response = _dispatch(router, workdir, FakeRegistry(session), monkeypatch)
    _ban_status_polls(router, monkeypatch)
    router.CLOSE_GRACE_SECONDS = FAST_GRACE

    _close(router, response["job_id"])

    record = router._read_record(response["job_id"])
    assert record["state"] in router.TERMINAL_STATES
    assert record["state"] != "closing"


def test_forced_close_does_not_claim_success_from_a_result_event(
    router, workdir, monkeypatch
):
    """A caller's close is not evidence that Claude finished its work."""
    session = FakeSession()
    session.output_buffer = RESULT_OK
    response = _dispatch(router, workdir, FakeRegistry(session), monkeypatch)
    _ban_status_polls(router, monkeypatch)
    router.CLOSE_GRACE_SECONDS = FAST_GRACE

    closed = _close(router, response["job_id"])

    assert closed["state"] == "closed_by_user"
    record = router._read_record(response["job_id"])
    assert record["state"] == "closed_by_user"
    # The classification the exit would otherwise have had is kept as audit
    # detail, and the close is never allowed to arm the recovery fence.
    assert str(record["outcome"]).startswith("closed_by_user_after_forced_termination(")
    assert not record.get("requires_explicit_recovery")
    assert record["terminal_report"]["termination_source"] == router.CLOSE_TERMINATION_SOURCE


# --------------------------------------------------------------------------
# 2. A child that exits inside the grace window is left alone
# --------------------------------------------------------------------------

def test_natural_exit_during_grace_is_not_force_killed(router, workdir, monkeypatch):
    session = FakeSession()
    registry = FakeRegistry(session)
    response = _dispatch(router, workdir, registry, monkeypatch)
    _ban_status_polls(router, monkeypatch)
    router.CLOSE_GRACE_SECONDS = 5.0
    exit_during_grace(session, delay=0.05, exit_code=0, output=RESULT_OK)

    started = time.monotonic()
    closed = _close(router, response["job_id"])

    assert registry.kills == [], "a child that exited on its own must not be killed"
    assert closed["state"] == "completed_unverified"
    assert closed["terminal"] is True
    assert closed["close_termination"]["mode"] == "graceful"
    assert closed["close_termination"]["forced"] is False
    assert time.monotonic() - started < 5.0, "close must return on the exit, not on the timeout"


def test_close_of_an_already_exited_process_kills_nothing(router, workdir, monkeypatch):
    session = FakeSession()
    registry = FakeRegistry(session)
    response = _dispatch(router, workdir, registry, monkeypatch)
    session.finish(exit_code=0, output=RESULT_OK)

    closed = _close(router, response["job_id"])

    assert registry.kills == []
    assert closed["state"] == "completed_unverified"
    assert closed["terminal"] is True


# --------------------------------------------------------------------------
# 3. Durable termination evidence
# --------------------------------------------------------------------------

def test_close_records_termination_request_evidence(router, workdir, monkeypatch):
    session = FakeSession()
    response = _dispatch(router, workdir, FakeRegistry(session), monkeypatch)
    router.CLOSE_GRACE_SECONDS = FAST_GRACE

    _close(router, response["job_id"])

    record = router._read_record(response["job_id"])
    assert isinstance(record["close_requested_at"], (int, float))
    termination = record["close_termination"]
    assert termination["stdin"] == "ok"
    assert termination["grace_seconds"] == FAST_GRACE
    assert termination["mode"] == "forced"
    assert termination["forced"] is True
    assert termination["kill_status"] == "killed"
    assert termination["termination_source"] == router.CLOSE_TERMINATION_SOURCE
    assert isinstance(record["close_forced_at"], (int, float))


def test_graceful_close_records_that_no_force_was_needed(router, workdir, monkeypatch):
    session = FakeSession()
    response = _dispatch(router, workdir, FakeRegistry(session), monkeypatch)
    router.CLOSE_GRACE_SECONDS = 5.0
    exit_during_grace(session, delay=0.05, exit_code=0, output=RESULT_OK)

    _close(router, response["job_id"])

    record = router._read_record(response["job_id"])
    assert record["close_termination"]["mode"] == "graceful"
    assert record["close_termination"]["forced"] is False
    assert "close_forced_at" not in record


# --------------------------------------------------------------------------
# 4. Exactly-once completion, and idempotence
# --------------------------------------------------------------------------

def test_close_never_consumes_the_single_completion_notification(
    router, workdir, monkeypatch
):
    session = FakeSession()
    registry = FakeRegistry(session)
    response = _dispatch(router, workdir, registry, monkeypatch)
    _ban_status_polls(router, monkeypatch)
    router.CLOSE_GRACE_SECONDS = FAST_GRACE
    polls_before = registry.polls

    _close(router, response["job_id"])

    assert registry.polls == polls_before, "close must not poll(): it suppresses the drain"
    assert registry.is_completion_consumed(session.id) is False
    assert session._completion_event.is_set() is True


def test_close_is_idempotent(router, workdir, monkeypatch):
    session = FakeSession()
    registry = FakeRegistry(session)
    response = _dispatch(router, workdir, registry, monkeypatch)
    job_id = response["job_id"]
    router.CLOSE_GRACE_SECONDS = FAST_GRACE

    first = _close(router, job_id)
    kills_after_first = len(registry.kills)
    second = _close(router, job_id)

    assert second["state"] == first["state"]
    assert second["terminal"] is True
    assert second["already_closed"] is True
    assert len(registry.kills) == kills_after_first, "a second close must not re-kill"
    record = router._read_record(job_id)
    assert record["completion_signal"]["recorded_at"] == first["completion_signal"]["recorded_at"]
    status_text = Path(record["status_file"]).read_text(encoding="utf-8")
    assert status_text.count("## Terminal outcome (Hermes supervisor)") == 1


def test_close_reports_transitional_closing_only_when_termination_fails(
    router, workdir, monkeypatch
):
    class StubbornRegistry(FakeRegistry):
        def kill_process(self, session_id, *, source="process.kill", consume_output=True):
            self.kills.append({"session_id": session_id, "source": source,
                               "consume_output": consume_output})
            return {"status": "error", "error": "runtime handle is no longer available"}

    session = FakeSession()
    registry = StubbornRegistry(session)
    response = _dispatch(router, workdir, registry, monkeypatch)
    job_id = response["job_id"]
    router.CLOSE_GRACE_SECONDS = FAST_GRACE

    closed = _close(router, job_id)

    assert closed["state"] == "closing"
    assert closed["terminal"] is False
    assert closed["close_termination"]["kill_status"] == "error"
    # Something must still be watching for the exit it could not force.
    assert job_id in router._RECONCILERS


# --------------------------------------------------------------------------
# 5. The real rail: a real child that ignores stdin
# --------------------------------------------------------------------------

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


def _real_job(router, registry, session, workdir: Path, runs_dir: Path, job_id: str) -> None:
    router._register_completion_watcher(session.id, {})
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


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except (ProcessLookupError, PermissionError):
        return False
    return True


def test_real_child_that_ignores_stdin_is_actually_terminated(
    real_registry, runs_dir, workdir, monkeypatch
):
    """The reported hang, reproduced: stdin closed, child working forever."""
    router = load_router(runs_dir)
    _ban_status_polls(router, monkeypatch)
    router.CLOSE_GRACE_SECONDS = 1.0

    session = real_registry.spawn_local(
        "while true; do sleep 0.2; done",
        cwd=str(workdir),
        keep_stdin_open=True,
    )
    pid = session.pid
    job_id = "claude-realhang01"
    _real_job(router, real_registry, session, workdir, runs_dir, job_id)

    closed = json.loads(router.handle_close({"job_id": job_id}))

    assert closed["terminal"] is True, closed
    assert closed["state"] == "closed_by_user"
    assert session.exited is True
    deadline = time.monotonic() + 10
    while _pid_alive(pid) and time.monotonic() < deadline:
        time.sleep(0.05)
    assert not _pid_alive(pid), "the managed child is still running after close"
    assert session.id not in real_registry._running

    record = router._read_record(job_id)
    assert record["state"] == "closed_by_user"
    assert record["close_termination"]["forced"] is True

    # The core still owns exactly one user-facing notification.
    completions = []
    while not real_registry.completion_queue.empty():
        event = real_registry.completion_queue.get_nowait()
        if event.get("type") == "completion" and event.get("session_id") == session.id:
            completions.append(event)
    assert len(completions) == 1, completions
    assert real_registry.is_completion_consumed(session.id) is False
    assert session.id not in real_registry._poll_observed


def test_real_child_exiting_during_grace_is_not_force_killed(
    real_registry, runs_dir, workdir, monkeypatch
):
    router = load_router(runs_dir)
    _ban_status_polls(router, monkeypatch)
    router.CLOSE_GRACE_SECONDS = 20.0

    result_line = RESULT_OK.strip()
    session = real_registry.spawn_local(
        f"cat > /dev/null; sleep 0.5; printf '%s\\n' '{result_line}'; exit 0",
        cwd=str(workdir),
        keep_stdin_open=True,
    )
    job_id = "claude-realgrace1"
    _real_job(router, real_registry, session, workdir, runs_dir, job_id)

    closed = json.loads(router.handle_close({"job_id": job_id}))

    assert closed["state"] == "completed_unverified", closed
    assert closed["close_termination"]["forced"] is False
    assert session.exit_code == 0
    assert real_registry.is_completion_consumed(session.id) is False
