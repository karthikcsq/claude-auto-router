"""Regressions for two delegation-reliability defects.

1. **Follow-up delivery framing.** Claude's ``--input-format stream-json`` is
   newline-delimited JSON: an event is only consumed once its terminating
   newline arrives. The launch pipe frames its first event explicitly with
   ``printf '%s\n'``; every queued follow-up must be framed the same way by the
   plugin itself instead of inheriting whatever line ending the transport's
   interactive "press Enter" helper happens to append (``\r\n`` on a Windows
   PTY, nothing at all through a raw write). Exactly one newline, never two —
   a blank line is an empty event to a line reader.

2. **Action-required reporting.** A turn that ends blocked on a human
   approval/permission decision is neither a normal ``waiting_for_update`` nor a
   generic failure. It must reach a distinct, durable, secret-safe state that
   never claims the permission was granted and never auto-approves.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from conftest import FakeRegistry, FakeSession, install_fake_rail

LAUNCH_OK = {"session_id": "proc_fake000000", "pid": 4242}

RESULT_OK = '{"type":"result","subtype":"success","num_turns":4,"duration_ms":900,"session_id":"s1"}\n'


def result_event(text: str, *, num_turns: int = 4, **extra) -> str:
    event = {
        "type": "result",
        "subtype": "success",
        "session_id": "s1",
        "num_turns": num_turns,
        "duration_ms": 900,
        "result": text,
    }
    event.update(extra)
    return json.dumps(event) + "\n"


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


def message(router, job_id: str, text: str) -> dict:
    return json.loads(router.handle_message({"job_id": job_id, "message": text}))


# ==========================================================================
# 1. Newline-delimited follow-up delivery
# ==========================================================================

def test_queued_follow_up_is_exactly_one_newline_delimited_event(
    router, workdir, monkeypatch
):
    """The bug: the follow-up went out unframed and was never consumed."""
    session = FakeSession()
    registry = FakeRegistry(session)
    response, _ = dispatch(router, workdir, registry, monkeypatch)

    queued = message(router, response["job_id"], "also add tests")

    assert queued["success"] is True and queued["queued"] is True
    assert len(registry.stdin_writes) == 1
    payload = registry.stdin_writes[0]
    # Exactly one logical event: one terminating newline, never two.
    assert payload.endswith("\n"), "stream-json event was submitted without its delimiter"
    assert payload.count("\n") == 1, f"expected one delimiter, got {payload.count(chr(10))}"
    assert not payload.endswith("\n\n")
    event = json.loads(payload)
    assert event["type"] == "user"
    assert event["message"] == {"role": "user", "content": "also add tests"}
    assert event["parent_tool_use_id"] is None


def test_consecutive_follow_ups_are_separately_consumable_events(
    router, workdir, monkeypatch
):
    """Concatenating the wire bytes must yield N parseable lines, in order."""
    session = FakeSession()
    registry = FakeRegistry(session)
    response, _ = dispatch(router, workdir, registry, monkeypatch)

    message(router, response["job_id"], "first follow-up")
    message(router, response["job_id"], "second follow-up")

    wire = "".join(registry.stdin_writes)
    lines = wire.split("\n")
    assert lines[-1] == "", "the stream must end on a delimiter"
    events = [json.loads(line) for line in lines[:-1]]
    assert [e["message"]["content"] for e in events] == [
        "first follow-up", "second follow-up",
    ]
    # No empty event was injected between them.
    assert "\n\n" not in wire


def test_stream_event_line_is_one_line_even_for_multiline_input(router):
    """Pure framing helper: embedded newlines are escaped, never emitted raw."""
    line = router._stream_user_event_line("line one\nline two\n\nline four ✅")

    assert line.count("\n") == 1 and line.endswith("\n")
    assert json.loads(line)["message"]["content"] == "line one\nline two\n\nline four ✅"
    # The framing helper is the launch payload plus exactly one delimiter.
    assert line == router._stream_user_message("line one\nline two\n\nline four ✅") + "\n"


def test_launch_pipe_and_follow_ups_use_the_same_framing(router, workdir, monkeypatch):
    """The first event and every later event must be framed identically."""
    session = FakeSession()
    registry = FakeRegistry(session)
    response, calls = dispatch(router, workdir, registry, monkeypatch)
    message(router, response["job_id"], "follow-up")

    assert "printf '%s\\n'" in calls[0]["command"]
    assert registry.stdin_writes[0].endswith("\n")


def test_legacy_registry_without_raw_write_still_gets_one_newline(
    router, workdir, monkeypatch
):
    """A rail that only exposes submit_stdin (which appends its own line
    ending) must receive an unterminated payload — otherwise the delimiter is
    doubled and a line reader sees a spurious empty event."""

    class LegacyRegistry(FakeRegistry):
        write_stdin = None  # type: ignore[assignment]

    session = FakeSession()
    registry = LegacyRegistry(session)
    response, _ = dispatch(router, workdir, registry, monkeypatch)

    message(router, response["job_id"], "legacy rail follow-up")

    payload = registry.stdin_writes[0]
    assert not payload.endswith("\n")
    assert json.loads(payload)["message"]["content"] == "legacy rail follow-up"


def test_failed_delivery_is_not_recorded_as_queued(router, workdir, monkeypatch):
    session = FakeSession()
    registry = FakeRegistry(session)
    response, _ = dispatch(router, workdir, registry, monkeypatch)

    def broken(session_id, data=""):
        return {"status": "error", "error": "pipe closed"}

    monkeypatch.setattr(registry, "write_stdin", broken)
    monkeypatch.setattr(registry, "submit_stdin", broken)
    replied = message(router, response["job_id"], "will not land")

    assert replied["success"] is False
    assert router._read_record(response["job_id"]).get("messages_queued") in (None, 0)


def test_follow_up_to_a_terminal_job_is_still_rejected_without_writing(
    router, workdir, monkeypatch
):
    """Existing rejection behaviour for dead/terminal jobs is preserved."""
    session = FakeSession()
    registry = FakeRegistry(session)
    response, _ = dispatch(router, workdir, registry, monkeypatch)
    session.finish(exit_code=1, output="crashed\n")

    replied = message(router, response["job_id"], "one more thing")

    assert replied["success"] is False
    assert replied["state"] == "failed_terminal"
    assert registry.stdin_writes == []


# ==========================================================================
# 2. Action-required normalization (pure)
# ==========================================================================

APPROVAL_TEXT = (
    "I finished the analysis but I am blocked: pushing the branch needs your "
    "explicit approval before I can continue."
)
PROSE_TEXT = (
    "I refactored the approval workflow module and documented how approval "
    "requests are routed. No approval is needed to run the suite, and the "
    "approval helpers now live in approvals.py. All 40 tests pass."
)


def test_structured_permission_denials_are_the_preferred_signal(router):
    detail = router._normalize_action_required(
        [], {"type": "result", "subtype": "success",
             "permission_denials": [{"tool_name": "Bash"}, {"tool_name": "Write"}]}
    )

    assert detail["blocked"] is True
    assert detail["signal"] == "permission_denials"
    assert detail["confidence"] == "structured"
    assert detail["tools"] == ["Bash", "Write"]
    assert detail["permission_granted"] is False
    assert detail["auto_approved"] is False


def test_pending_control_request_after_the_last_result_is_structured(router):
    events = [
        {"type": "result", "subtype": "success"},
        {"type": "control_request", "request": {"subtype": "can_use_tool", "tool_name": "Bash"}},
    ]

    detail = router._normalize_action_required(events, {"type": "result", "subtype": "success"})

    assert detail["signal"] == "control_request"
    assert detail["confidence"] == "structured"
    assert detail["tools"] == ["Bash"]


def test_a_control_request_already_answered_before_the_result_is_not_a_block(router):
    events = [
        {"type": "control_request", "request": {"subtype": "can_use_tool", "tool_name": "Bash"}},
        {"type": "result", "subtype": "success", "result": "Done. All tests pass."},
    ]

    assert router._normalize_action_required(events, events[-1]) is None


def test_narrow_text_detection_finds_an_explicit_block(router):
    detail = router._normalize_action_required([], {"type": "result", "result": APPROVAL_TEXT})

    assert detail["blocked"] is True
    assert detail["signal"] == "result_text"
    assert detail["confidence"] == "text"
    assert "approval" in detail["summary"].lower()


def test_approval_merely_discussed_in_prose_is_not_a_block(router):
    """The false positive that would make the state meaningless."""
    assert router._normalize_action_required([], {"type": "result", "result": PROSE_TEXT}) is None


@pytest.mark.parametrize("text", [
    "The task asked me to add approval handling to the router; that is done.",
    "Approval was granted earlier, so I proceeded with the migration.",
    "Once approval lands I will publish, but nothing here is blocked.",
    "This change does not require approval from anyone.",
    "I documented the permissions model and the approval matrix in README.md.",
    "",
])
def test_non_blocking_text_never_trips_the_detector(router, text):
    assert router._normalize_action_required([], {"type": "result", "result": text}) is None


@pytest.mark.parametrize("text", [
    "I am blocked on approval to force-push to main.",
    "Claude requested permissions to use Bash, but you haven't granted it yet.",
    "I cannot proceed without your permission to delete the staging bucket.",
    "Please approve the deployment before I continue.",
])
def test_explicit_blocking_text_is_detected(router, text):
    detail = router._normalize_action_required([], {"type": "result", "result": text})
    assert detail is not None and detail["blocked"] is True


def test_normalization_is_pure_and_repeatable(router):
    events = [{"type": "result", "subtype": "success", "result": APPROVAL_TEXT}]
    snapshot = json.loads(json.dumps(events))

    first = router._normalize_action_required(events, events[0])
    second = router._normalize_action_required(events, events[0])

    assert first == second
    assert events == snapshot, "the normalizer mutated its inputs"


# ==========================================================================
# 3. Action-required lifecycle (durable state, status, transitions)
# ==========================================================================

def blocked_job(router, workdir, monkeypatch, *, output=None):
    session = FakeSession()
    registry = FakeRegistry(session)
    response, _ = dispatch(router, workdir, registry, monkeypatch)
    session.output_buffer = output if output is not None else result_event(APPROVAL_TEXT)
    return session, registry, response["job_id"]


def test_blocked_turn_reaches_a_distinct_non_terminal_state(
    router, workdir, monkeypatch
):
    _, _, job_id = blocked_job(router, workdir, monkeypatch)

    reported = status(router, job_id)

    assert reported["state"] == "action_required"
    assert reported["action_required"] is True
    assert reported["terminal"] is False
    assert reported["requires_explicit_recovery"] is False
    detail = reported["action_required_detail"]
    assert detail["summary"]
    assert detail["permission_granted"] is False
    assert detail["auto_approved"] is False
    assert "auto-approve" in detail["guidance"]
    assert detail["detected_at"]
    # Durable, not just in the response.
    assert router._read_record(job_id)["action_required"]["blocked"] is True


def test_a_normal_success_turn_still_reports_waiting_for_update(
    router, workdir, monkeypatch
):
    """Guarantee 3: the ordinary result path is untouched."""
    _, _, job_id = blocked_job(
        router, workdir, monkeypatch,
        output=result_event("Implemented the parser and all 40 tests pass."),
    )

    reported = status(router, job_id)

    assert reported["state"] == "waiting_for_update"
    assert reported["action_required"] is False
    assert reported["action_required_detail"] is None


def test_prose_mentioning_approval_stays_waiting_for_update(
    router, workdir, monkeypatch
):
    _, _, job_id = blocked_job(router, workdir, monkeypatch, output=result_event(PROSE_TEXT))

    assert status(router, job_id)["state"] == "waiting_for_update"


def test_action_required_is_recorded_compactly_in_the_status_file(
    router, workdir, monkeypatch
):
    _, _, job_id = blocked_job(router, workdir, monkeypatch)
    status(router, job_id)

    text = Path(router._read_record(job_id)["status_file"]).read_text()

    assert router.ACTION_REQUIRED_MARKER in text
    assert "action_required" in text
    assert len(text) < router.MAX_STATUS_CHARS
    # Repeated polls must not append the section again.
    status(router, job_id)
    status(router, job_id)
    text = Path(router._read_record(job_id)["status_file"]).read_text()
    assert text.count(router.ACTION_REQUIRED_MARKER) == 1


def test_action_required_is_sticky_across_polls_and_blind_spots(
    router, workdir, monkeypatch
):
    session, registry, job_id = blocked_job(router, workdir, monkeypatch)
    assert status(router, job_id)["state"] == "action_required"

    # Re-poll with the same transcript: still blocked, not downgraded.
    assert status(router, job_id)["state"] == "action_required"

    # "Cannot look" must not silently clear it either.
    monkeypatch.setitem(__import__("sys").modules, "tools.process_registry", None)
    assert status(router, job_id)["state"] == "action_required"


def test_a_later_clean_turn_result_resolves_the_block(router, workdir, monkeypatch):
    session, _, job_id = blocked_job(router, workdir, monkeypatch)
    assert status(router, job_id)["state"] == "action_required"

    session.output_buffer += result_event("Approved path taken; done.", num_turns=9)
    reported = status(router, job_id)

    assert reported["state"] == "waiting_for_update"
    assert reported["action_required"] is False
    resolved = router._read_record(job_id)["action_required"]
    assert resolved["resolved_at"] and resolved["resolved_by"] == "later_turn_result"


def test_a_user_follow_up_transitions_out_of_action_required(
    router, workdir, monkeypatch
):
    session, registry, job_id = blocked_job(router, workdir, monkeypatch)
    assert status(router, job_id)["state"] == "action_required"

    replied = message(router, job_id, "Approved: you may force-push that branch.")

    assert replied["success"] is True
    assert replied["previous_state"] == "action_required"
    assert replied["cleared_action_required"] is True
    record = router._read_record(job_id)
    assert record["state"] == "running"
    assert record["action_required"]["resolved_by"] == "user_follow_up"
    assert record["action_required"]["resolved_at"]
    # The transition is persisted for the supervisor too.
    assert "Resolved" in Path(record["status_file"]).read_text()
    # And the follow-up itself was framed correctly.
    assert registry.stdin_writes[0].count("\n") == 1


def test_action_required_still_occupies_the_one_parent_per_turn_slot(
    router, workdir, monkeypatch
):
    _, _, job_id = blocked_job(router, workdir, monkeypatch)
    status(router, job_id)

    second, _ = dispatch(router, workdir, FakeRegistry(FakeSession()), monkeypatch)

    assert second["success"] is False
    assert "One parent job per turn" in second["error"]


def test_terminal_failure_after_action_required_reports_both(
    router, workdir, monkeypatch
):
    session, _, job_id = blocked_job(router, workdir, monkeypatch)
    assert status(router, job_id)["state"] == "action_required"

    session.finish(exit_code=1, reason="exited", output=session.output_buffer + "fatal: aborted\n")
    reported = status(router, job_id)

    assert reported["state"] == "failed_terminal"
    assert reported["terminal"] is True
    assert reported["requires_explicit_recovery"] is True
    report = reported["terminal_report"]
    assert report["action_required"]["summary"]
    assert "approval" in report["summary"].lower()
    text = Path(router._read_record(job_id)["status_file"]).read_text()
    assert "Terminal outcome (Hermes supervisor)" in text
    assert router.ACTION_REQUIRED_MARKER in text


def test_a_resolved_block_does_not_taint_a_later_terminal_report(
    router, workdir, monkeypatch
):
    session, _, job_id = blocked_job(router, workdir, monkeypatch)
    status(router, job_id)
    message(router, job_id, "Approved, continue.")

    session.finish(exit_code=0, reason="exited", output=session.output_buffer + RESULT_OK)
    reported = status(router, job_id)

    assert reported["state"] == "completed_unverified"
    assert "action_required" not in reported["terminal_report"]


def test_action_required_detail_never_echoes_a_raw_secret(
    router, workdir, monkeypatch
):
    secret = "sk-ant-api03-" + "B" * 40
    text = (
        f"I am blocked on your approval to publish with token {secret} — "
        "confirm before I continue."
    )
    _, _, job_id = blocked_job(router, workdir, monkeypatch, output=result_event(text))

    reported = status(router, job_id)

    assert reported["state"] == "action_required"
    blob = json.dumps(reported) + Path(reported["supervisor_status"]["path"]).read_text()
    assert secret not in blob
    assert secret not in json.dumps(router._read_record(job_id))


def test_redaction_failure_withholds_action_evidence_entirely(router, monkeypatch):
    monkeypatch.setitem(__import__("sys").modules, "agent.redact", None)

    detail = router._normalize_action_required(
        [], {"type": "result", "result": "I am blocked on approval; token=hunter2-super-secret"}
    )

    assert detail is not None
    assert "hunter2" not in json.dumps(detail)
    assert "withheld" in detail["evidence"]


def test_task_prose_replayed_as_a_user_event_is_never_scanned(router):
    """`--replay-user-messages` echoes the task back; it is not Claude's turn text."""
    events = [
        {"type": "user", "message": {"role": "user", "content":
            "I am blocked on approval to force-push; build the approval workflow."}},
        {"type": "result", "subtype": "success",
         "result": "Implemented the approval workflow; all tests pass."},
    ]

    assert router._normalize_action_required(events, events[-1]) is None
