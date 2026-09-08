"""Regression suite for Claude-delegation completion/error callback reliability.

Every terminal path a delegated job can take must (a) persist a sticky terminal
job state, (b) carry a completion signal the originating conversation can act
on, and (c) never report success on the strength of a result event or an exit
code alone.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from conftest import FakeRegistry, FakeSession, exit_during_grace, install_fake_rail

RESULT_OK = '{"type":"result","subtype":"success","num_turns":4,"duration_ms":900}\n'
RESULT_ERR = '{"type":"result","subtype":"error_during_execution","num_turns":2}\n'
LAUNCH_OK = {"session_id": "proc_fake000000", "pid": 4242}


def dispatch(router, workdir: Path, registry: FakeRegistry, monkeypatch, *, launch=None):
    calls = install_fake_rail(monkeypatch, registry, launch or LAUNCH_OK)
    response = json.loads(router.handle_dispatch(
        {"task": "do the work", "workdir": str(workdir)},
        task_id="turn-1",
        session_id="sess-parent",
    ))
    return response, calls


def status(router, job_id: str) -> dict:
    return json.loads(router.handle_status({"job_id": job_id}))


# --------------------------------------------------------------------------
# 1. Callback registration — the exact wiring, not a claim about it
# --------------------------------------------------------------------------

def test_dispatch_registers_a_completion_watcher_alongside_the_result_watch(
    router, workdir, monkeypatch
):
    """A result-event watch must not be the only notification rail."""
    session = FakeSession()
    registry = FakeRegistry(session)
    response, calls = dispatch(router, workdir, registry, monkeypatch)

    assert response["success"] is True
    # The live per-turn signal is preserved...
    assert calls[0]["watch_patterns"] == ['"type":"result"']
    assert calls[0]["keep_stdin_open"] is True
    assert calls[0]["background"] is True
    # ...and the terminal signal is armed on the same managed session.
    assert session.notify_on_complete is True
    assert session.watcher_interval == router.COMPLETION_WATCH_INTERVAL_SECONDS
    assert registry.pending_watchers == [{
        "session_id": "proc_fake000000",
        "check_interval": router.COMPLETION_WATCH_INTERVAL_SECONDS,
        "session_key": "agent:main:discord:chat-1",
        "platform": "discord",
        "chat_id": "chat-1",
        "user_id": "user-1",
        "user_name": "karthik",
        "thread_id": "thread-1",
        "message_id": "msg-1",
        "notify_on_complete": True,
        "parent_session_id": "sess-parent",
    }]
    # Re-checkpointed so a gateway restart re-arms the watcher.
    assert registry.checkpoints == 1


def test_registered_watcher_carries_the_routing_of_the_originating_conversation(
    router, workdir, monkeypatch
):
    session = FakeSession()
    registry = FakeRegistry(session)
    dispatch(router, workdir, registry, monkeypatch)

    watcher = registry.pending_watchers[0]
    # gateway/run.py::_run_process_watcher reads exactly these keys to build and
    # route the completion event.
    for key in ("session_id", "check_interval", "notify_on_complete", "platform",
                "chat_id", "thread_id", "user_id", "user_name", "message_id",
                "parent_session_id", "session_key"):
        assert key in watcher
    assert watcher["parent_session_id"] == session.parent_session_id


def test_record_reports_completion_callback_truthfully_when_unsupported(
    router, workdir, monkeypatch
):
    """A finite session cannot deliver async completions — say so, don't pretend."""
    session = FakeSession(routed=False)
    registry = FakeRegistry(session)
    response, _ = dispatch(
        router, workdir, registry, monkeypatch,
        launch={**LAUNCH_OK, "notify_unsupported": "one-shot runner"},
    )

    assert response["success"] is True
    assert registry.pending_watchers == []
    assert session.notify_on_complete is False
    assert "Poll claude_code_status" in response["completion_callback"]
    record = router._read_record(response["job_id"])
    assert record["completion_callback"] is False
    assert status(router, response["job_id"])["completion_callback"] is False


def test_missing_managed_session_is_reported_not_silently_assumed(
    router, workdir, monkeypatch
):
    registry = FakeRegistry(None)
    response, _ = dispatch(router, workdir, registry, monkeypatch)

    record = router._read_record(response["job_id"])
    assert record["completion_callback"] is False
    assert "not found" in record["completion_watcher"]["reason"]


# --------------------------------------------------------------------------
# 2. Terminal paths — each persists a sticky state and a completion signal
# --------------------------------------------------------------------------

TERMINAL_CASES = [
    # (label, exit_code, reason, output, expected_state)
    ("successful result then exit", 0, "exited", RESULT_OK, "completed_unverified"),
    ("process failure with no result", 1, "exited", "claude: fatal error\n", "failed_terminal"),
    ("malformed output, no result event", 0, "exited", "not json at all\n", "unknown_needs_reconciliation"),
    ("result reports error", 0, "exited", RESULT_ERR, "failed_terminal"),
    ("success result but dirty exit", 3, "exited", RESULT_OK, "failed_terminal"),
    ("killed mid-run", -9, "killed", RESULT_OK, "failed_terminal"),
    ("workers system-killed", 0, "exited", RESULT_OK + "7 spawned, 0 completed, 7 system-killed.\n",
     "incomplete_recoverable"),
]


@pytest.mark.parametrize("label,exit_code,reason,output,expected", TERMINAL_CASES)
def test_every_terminal_path_persists_state_and_a_completion_signal(
    router, workdir, monkeypatch, label, exit_code, reason, output, expected
):
    session = FakeSession()
    registry = FakeRegistry(session)
    response, _ = dispatch(router, workdir, registry, monkeypatch)
    job_id = response["job_id"]

    session.finish(exit_code=exit_code, reason=reason, output=output)
    reported = status(router, job_id)

    assert reported["state"] == expected, label
    assert reported["terminal"] is True, label
    record = router._read_record(job_id)
    assert record["state"] == expected
    assert record["completed_at"]
    assert record["completion_signal"]["outcome"] == expected
    assert record["terminal_report"]["summary"]
    # A terminal state is durable on disk, not just in the response.
    assert "Terminal outcome (Hermes supervisor)" in Path(record["status_file"]).read_text()
    if expected != "completed_unverified":
        assert record["requires_explicit_recovery"] is True
        assert reported["recovery_guidance"]


def test_no_result_failure_is_reported_explicitly(router, workdir, monkeypatch):
    session = FakeSession()
    registry = FakeRegistry(session)
    response, _ = dispatch(router, workdir, registry, monkeypatch)
    session.finish(exit_code=1, reason="exited", output="Error: could not authenticate\n")

    reported = status(router, response["job_id"])
    report = reported["terminal_report"]

    assert report["result_event_seen"] is False
    assert "no `\"type\":\"result\"` event" in report["no_result_event"]
    assert report["exit_code"] == 1
    status_text = Path(reported["supervisor_status"]["path"]).read_text()
    assert "**Claude result event:** NO" in status_text


def test_dispatch_resolves_a_process_that_died_before_returning(
    router, workdir, monkeypatch
):
    """The launch "succeeded" but the child was already gone."""
    session = FakeSession()
    session.finish(exit_code=127, output="claude: command not found\n")
    registry = FakeRegistry(session)
    response, _ = dispatch(router, workdir, registry, monkeypatch)

    assert response["success"] is False
    assert response["state"] == "failed_terminal"
    assert response["requires_explicit_recovery"] is True
    assert router._read_record(response["job_id"])["state"] == "failed_terminal"
    # Still watched, so the notification rail also fires — never rely on the
    # tool result alone.
    assert registry.pending_watchers[0]["notify_on_complete"] is True


def test_close_then_exit_reaches_a_terminal_state(router, workdir, monkeypatch):
    session = FakeSession()
    registry = FakeRegistry(session)
    response, _ = dispatch(router, workdir, registry, monkeypatch)
    job_id = response["job_id"]

    # Claude takes the EOF and ends its own turn inside the close grace, so the
    # close never has to force anything and the exit classifies normally.
    session.output_buffer = RESULT_OK
    exit_during_grace(session, exit_code=0, output=RESULT_OK)
    closed = json.loads(router.handle_close({"job_id": job_id}))

    assert closed["state"] == "completed_unverified" and closed["terminal"] is True
    assert registry.stdin_closed is True
    assert registry.kills == []
    assert status(router, job_id)["state"] == "completed_unverified"


def test_close_after_claude_already_exited_does_not_stick_in_closing(
    router, workdir, monkeypatch
):
    """The dominant stuck state in the historical run records."""
    session = FakeSession()
    registry = FakeRegistry(session)
    response, _ = dispatch(router, workdir, registry, monkeypatch)
    session.finish(exit_code=0, output=RESULT_OK)

    closed = json.loads(router.handle_close({"job_id": response["job_id"]}))

    assert closed["state"] == "completed_unverified"
    assert closed["terminal"] is True


# --------------------------------------------------------------------------
# 3. Durability / reconciliation after a restart
# --------------------------------------------------------------------------

def test_lost_managed_session_reconciles_to_unknown_not_running(
    router, workdir, monkeypatch
):
    """Gateway restart without process recovery: the registry no longer knows it."""
    session = FakeSession()
    registry = FakeRegistry(session)
    response, _ = dispatch(router, workdir, registry, monkeypatch)
    registry.session = None  # session gone after restart

    reported = status(router, response["job_id"])

    assert reported["state"] == "unknown_needs_reconciliation"
    assert reported["requires_explicit_recovery"] is True
    assert reported["terminal_report"]["result_event_seen"] is False


def test_a_previously_seen_result_event_never_becomes_success_after_a_restart(
    router, workdir, monkeypatch
):
    session = FakeSession()
    registry = FakeRegistry(session)
    response, _ = dispatch(router, workdir, registry, monkeypatch)
    session.output_buffer = RESULT_OK
    assert status(router, response["job_id"])["state"] == "waiting_for_update"

    registry.session = None
    assert status(router, response["job_id"])["state"] == "unknown_needs_reconciliation"


def test_stale_unknown_is_dated_at_last_known_activity(router, workdir, monkeypatch):
    """A record reconciled with no exit evidence must not arm the 24h fence now."""
    session = FakeSession()
    registry = FakeRegistry(session)
    response, _ = dispatch(router, workdir, registry, monkeypatch)
    record = router._read_record(response["job_id"])
    record["started_at"] = record["started_at"] - 10 * 24 * 3600
    router._write_record(record)
    registry.session = None

    status(router, response["job_id"])

    reconciled = router._read_record(response["job_id"])
    assert reconciled["completed_at"] == reconciled["started_at"]


def test_terminal_state_is_sticky_across_later_polls(router, workdir, monkeypatch):
    session = FakeSession()
    registry = FakeRegistry(session)
    response, _ = dispatch(router, workdir, registry, monkeypatch)
    job_id = response["job_id"]
    session.finish(exit_code=1, output="boom\n")
    assert status(router, job_id)["state"] == "failed_terminal"

    # A later restart that "revives" a session id must not rewrite history.
    revived = FakeSession()
    registry.session = revived
    assert status(router, job_id)["state"] == "failed_terminal"


def test_record_write_is_atomic(router, runs_dir):
    router._write_record({"job_id": "claude-atomic", "state": "running"})

    assert (runs_dir / "claude-atomic.json").exists()
    assert not list(runs_dir.glob("*.tmp"))


# --------------------------------------------------------------------------
# 4. Secret safety of the supervisor report
# --------------------------------------------------------------------------

def test_terminal_report_never_echoes_a_raw_secret(router, workdir, monkeypatch):
    secret = "sk-ant-api03-" + "A" * 40
    session = FakeSession()
    registry = FakeRegistry(session)
    response, _ = dispatch(router, workdir, registry, monkeypatch)
    session.finish(exit_code=1, output=f"Authorization: Bearer {secret}\nfatal\n")

    reported = status(router, response["job_id"])

    blob = json.dumps(reported) + Path(reported["supervisor_status"]["path"]).read_text()
    assert secret not in blob
    assert "Bearer " + secret not in blob


def test_redaction_fails_closed_when_unavailable(router, monkeypatch):
    """No redactor must mean no evidence — never raw output."""
    import sys as _sys

    monkeypatch.setitem(_sys.modules, "agent.redact", None)
    out = router._terminal_evidence("token=hunter2-super-secret\n")

    assert "hunter2" not in out
    assert "withheld" in out


# --------------------------------------------------------------------------
# 5. Existing safeguards must survive
# --------------------------------------------------------------------------

def test_live_follow_up_messages_still_work(router, workdir, monkeypatch):
    session = FakeSession()
    registry = FakeRegistry(session)
    response, _ = dispatch(router, workdir, registry, monkeypatch)

    queued = json.loads(router.handle_message(
        {"job_id": response["job_id"], "message": "also add tests"}
    ))

    assert queued["success"] is True and queued["queued"] is True
    assert json.loads(registry.stdin_writes[0])["message"]["content"] == "also add tests"
    inbox = Path(router._read_record(response["job_id"])["inbox_file"]).read_text()
    assert "also add tests" in inbox


def test_follow_up_to_a_dead_job_records_its_terminal_state(router, workdir, monkeypatch):
    session = FakeSession()
    registry = FakeRegistry(session)
    response, _ = dispatch(router, workdir, registry, monkeypatch)
    session.finish(exit_code=1, output="crashed\n")

    replied = json.loads(router.handle_message(
        {"job_id": response["job_id"], "message": "one more thing"}
    ))

    assert replied["success"] is False
    assert replied["state"] == "failed_terminal"
    assert router._read_record(response["job_id"])["state"] == "failed_terminal"


def test_one_parent_per_turn_guard_still_blocks(router, workdir, monkeypatch):
    session = FakeSession()
    registry = FakeRegistry(session)
    dispatch(router, workdir, registry, monkeypatch)

    second, _ = dispatch(router, workdir, registry, monkeypatch)

    assert second["success"] is False
    assert "One parent job per turn" in second["error"]


def test_fable_recovery_fence_arms_on_the_real_failed_state(router, tmp_path):
    """`failed_terminal` — what the classifier actually emits — must fence."""
    router._write_record({
        "job_id": "claude-fable", "model": "fable", "state": "failed_terminal",
        "repo_id": "repo-a", "completed_at": router.time.time(),
    })

    blocked = router._dispatch_guard(
        origin_task_id="next-turn", repo_id="repo-a", recovery_of_job_id=""
    )

    assert blocked and "Do not automatically retry, downgrade, or fan out" in blocked


def test_closed_stream_with_a_missing_process_is_not_an_unexplained_failure(
    router, workdir, monkeypatch
):
    """`closing` + gone process = the coordinator's own verified close.

    A close now terminates the session itself, so a record only sits at
    `closing` when termination could not be completed here (a gateway restart
    mid-close, or a recovered session with no runtime handle). That record must
    still resolve as a close, never as an unexplained failure.
    """
    session = FakeSession()
    registry = FakeRegistry(session)
    response, _ = dispatch(router, workdir, registry, monkeypatch)
    stuck = router._read_record(response["job_id"])
    stuck["state"] = "closing"
    stuck["closed_at"] = stuck["started_at"]
    router._write_record(stuck)
    registry.session = None  # e.g. gateway restarted after the close

    reported = status(router, response["job_id"])

    assert reported["state"] == "closed_by_user"
    assert reported["terminal"] is True
    record = router._read_record(response["job_id"])
    assert record["outcome"] == "closed_by_user_after_missing_process"
    # Not a failure: it must not arm the Fable recovery fence.
    assert record.get("requires_explicit_recovery") is not True
    assert record["state"] not in router.RECOVERY_FENCED_STATES


def test_recovery_flag_tracks_exactly_the_fenced_states(router, workdir, monkeypatch):
    for exit_code, output, fenced in [
        (0, RESULT_OK, False),
        (1, "boom\n", True),
        (0, "garbage\n", True),
    ]:
        session = FakeSession()
        registry = FakeRegistry(session)
        response, _ = dispatch(router, workdir, registry, monkeypatch)
        session.finish(exit_code=exit_code, output=output)
        reported = status(router, response["job_id"])
        assert reported["requires_explicit_recovery"] is fenced, reported["state"]
        assert (reported["state"] in router.RECOVERY_FENCED_STATES) is fenced
        # each iteration is its own coordinator turn
        record = router._read_record(response["job_id"])
        record["origin_task_id"] = None
        router._write_record(record)


def test_unreachable_registry_never_invents_a_terminal_state(router, workdir, monkeypatch):
    """"Cannot look" must not be recorded as "job is gone"."""
    session = FakeSession()
    registry = FakeRegistry(session)
    response, _ = dispatch(router, workdir, registry, monkeypatch)

    monkeypatch.setitem(__import__("sys").modules, "tools.process_registry", None)
    reported = status(router, response["job_id"])

    assert reported["state"] == "running"
    assert reported["terminal"] is False
