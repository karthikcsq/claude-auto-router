"""Explicit replacement for a session whose stdin was closed too early.

`claude_code_close` is irreversible: stdin cannot be reopened, so a coordinator
that closes a job before its requirements were complete has no way back. The
remedy is a deliberate, single replacement lineage — never an automatic retry,
never a fan-out, and never a silent claim that a still-terminating source was
restarted.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from conftest import FakeRegistry, FakeSession, exit_during_grace, install_fake_rail

RESULT_OK = '{"type":"result","subtype":"success","num_turns":4,"duration_ms":900}\n'
TURN = "turn-7"
NEW_TASK = "finish the migration and add the regression test we forgot"


class MultiRegistry(FakeRegistry):
    """A rail that can hold the source session and its replacement at once."""

    def __init__(self, *sessions: FakeSession):
        super().__init__(sessions[0] if sessions else None)
        self.sessions = {session.id: session for session in sessions}

    def get(self, session_id: str):
        return self.sessions.get(session_id)

    def add(self, session: FakeSession) -> FakeSession:
        self.sessions[session.id] = session
        return session


@pytest.fixture
def rail(router, workdir, monkeypatch):
    """A dispatched source job on a rail that can also launch its replacement."""
    source_session = FakeSession("proc_source0000")
    registry = MultiRegistry(source_session)
    launch = {"session_id": source_session.id, "pid": 4242}
    calls = install_fake_rail(monkeypatch, registry, launch)
    router.CLOSE_GRACE_SECONDS = 0.1
    response = json.loads(router.handle_dispatch(
        {"task": "do the original work", "workdir": str(workdir), "effort": "xhigh",
         "max_turns": 42, "permission_mode": "auto"},
        task_id=TURN, session_id="sess-parent",
    ))
    return type("Rail", (), {
        "router": router, "registry": registry, "launch": launch, "calls": calls,
        "session": source_session, "job_id": response["job_id"], "workdir": workdir,
    })


def _next_session(rail, session_id: str = "proc_child0000") -> FakeSession:
    """Point the fake launcher at a fresh managed session for the replacement."""
    session = rail.registry.add(FakeSession(session_id))
    rail.launch["session_id"] = session_id
    return session


def _restart(router, source_job_id: str, task: str = NEW_TASK, **extra) -> dict:
    payload = {"source_job_id": source_job_id, "task": task}
    payload.update(extra)
    return json.loads(router.handle_restart(payload, task_id=TURN, session_id="sess-parent"))


def _record(router, *, job_id: str, workdir: Path, **updates) -> dict:
    record: dict = {
        "job_id": job_id,
        "state": "running",
        "model": "opus",
        "started_at": time.time(),
        "workdir": str(workdir),
        "repo_id": router._repo_identity(workdir),
        "origin_task_id": TURN,
        "process_session_id": "proc_source0000",
    }
    record.update(updates)
    router._write_record(record)
    return record


# --------------------------------------------------------------------------
# 1. What may never be restarted
# --------------------------------------------------------------------------

@pytest.mark.parametrize("state", ["starting", "running", "waiting_for_update", "action_required"])
def test_restart_rejects_a_live_source(router, workdir, state):
    _record(router, job_id="claude-live", workdir=workdir, state=state)

    answer = _restart(router, "claude-live")

    assert answer["success"] is False
    assert answer.get("restarted") is False
    assert "claude_code_message" in answer["error"]


def test_restart_rejects_an_unknown_source(router, workdir):
    answer = _restart(router, "claude-nosuchjob")

    assert answer["success"] is False
    assert "unknown" in answer["error"].lower()


def test_restart_requires_an_explicit_new_task(router, workdir):
    _record(router, job_id="claude-closed", workdir=workdir,
            state="closed_by_user", closed_at=time.time())

    answer = json.loads(router.handle_restart({"source_job_id": "claude-closed"}, task_id=TURN))

    assert answer["success"] is False
    assert "task" in answer["error"]


def test_restart_rejects_a_source_that_was_never_closed(router, workdir):
    """A job that ended on its own is not an interrupted job."""
    _record(router, job_id="claude-done", workdir=workdir, state="completed_unverified")

    answer = _restart(router, "claude-done")

    assert answer["success"] is False
    assert "claude_code_close" in answer["error"]


def test_restart_rejects_a_failed_source_governed_by_the_recovery_fence(router, workdir):
    _record(router, job_id="claude-failed", workdir=workdir,
            state="failed_terminal", closed_at=time.time(), completed_at=time.time())

    answer = _restart(router, "claude-failed")

    assert answer["success"] is False
    assert "recovery" in answer["error"].lower()


def test_restart_rejects_a_fable_source(router, workdir):
    _record(router, job_id="claude-fable", workdir=workdir, model="fable",
            state="closed_by_user", closed_at=time.time())

    answer = _restart(router, "claude-fable")

    assert answer["success"] is False
    assert "fable" in answer["error"].lower()


def test_restart_rejects_a_parallel_lane_argument(router, workdir):
    _record(router, job_id="claude-closed", workdir=workdir,
            state="closed_by_user", closed_at=time.time())

    answer = _restart(router, "claude-closed", parallel_lane="implementation")

    assert answer["success"] is False
    assert "parallel_lane" in answer["error"]


# --------------------------------------------------------------------------
# 2. A source that is closing but still alive
# --------------------------------------------------------------------------

def test_closing_source_with_a_live_process_defers_instead_of_restarting(rail):
    router = rail.router
    record = router._read_record(rail.job_id)
    record["state"] = "closing"
    record["closed_at"] = time.time()
    router._write_record(record)

    answer = _restart(router, rail.job_id)

    assert answer["success"] is False
    assert answer["restarted"] is False
    assert answer["action"] == "restart_pending_source_exit"
    assert answer.get("job_id") is None
    # The source is left strictly alone.
    assert rail.session.exited is False
    assert rail.registry.kills == []
    assert len(rail.calls) == 1, "no replacement process may be launched yet"
    # ...and the intent is durable.
    pending = router._read_record(rail.job_id)["restart_pending"]
    assert pending["requested_at"] and pending["requested_by_task_id"] == TURN


def test_pending_restart_is_surfaced_by_status_and_list(rail):
    router = rail.router
    record = router._read_record(rail.job_id)
    record["state"] = "closing"
    record["closed_at"] = time.time()
    router._write_record(record)
    _restart(router, rail.job_id)

    status = json.loads(router.handle_status({"job_id": rail.job_id}))
    assert status["restart_action"] == "restart_pending_source_exit"

    row = json.loads(router.handle_list({}))["sessions"][0]
    assert row["job_id"] == rail.job_id
    assert row["restart_pending"] is True


def test_pending_restart_launches_once_the_source_has_exited(rail):
    router = rail.router
    record = router._read_record(rail.job_id)
    record["state"] = "closing"
    record["closed_at"] = time.time()
    router._write_record(record)
    deferred = _restart(router, rail.job_id)
    assert deferred["action"] == "restart_pending_source_exit"

    # The source finally exits; nothing killed it.
    rail.session.finish(exit_code=0, output=RESULT_OK)
    _next_session(rail)
    answer = _restart(router, rail.job_id)

    assert answer["success"] is True and answer["restarted"] is True
    assert answer["job_id"] != rail.job_id
    assert router._read_record(rail.job_id)["state"] in router.TERMINAL_STATES


# --------------------------------------------------------------------------
# 3. A completed close is immediately eligible
# --------------------------------------------------------------------------

def test_close_completed_source_restarts_without_a_pending_step(rail):
    router = rail.router
    closed = json.loads(router.handle_close({"job_id": rail.job_id}))
    assert closed["terminal"] is True
    _next_session(rail)

    answer = _restart(router, rail.job_id)

    assert answer["success"] is True
    assert answer["restarted"] is True
    assert "action" not in answer or answer["action"] != "restart_pending_source_exit"
    assert answer["source_job_id"] == rail.job_id


def test_restart_creates_exactly_one_replacement_lineage(rail):
    router = rail.router
    source_before = json.loads(router.handle_close({"job_id": rail.job_id}))
    _next_session(rail)

    answer = _restart(router, rail.job_id)
    child = router._read_record(answer["job_id"])
    source = router._read_record(rail.job_id)

    assert child["restart_of_job_id"] == rail.job_id
    assert source["restarted_by_job_id"] == answer["job_id"]
    assert isinstance(source["restart_requested_at"], (int, float))
    # The source's own terminal evidence is preserved untouched.
    assert source["state"] == source_before["state"]
    assert source["terminal_report"] == source_before["terminal_report"]
    assert source["completion_signal"]["recorded_at"] == source_before["completion_signal"]["recorded_at"]
    assert source["close_termination"]["forced"] is True


def test_a_second_restart_of_the_same_source_is_rejected(rail):
    router = rail.router
    router.handle_close({"job_id": rail.job_id})
    _next_session(rail)
    first = _restart(router, rail.job_id)
    assert first["success"] is True

    second = _restart(router, rail.job_id, task="another go")

    assert second["success"] is False
    assert first["job_id"] in second["error"]


def test_restarting_a_replacement_again_is_rejected(rail):
    router = rail.router
    router.handle_close({"job_id": rail.job_id})
    child_session = _next_session(rail)
    child_id = _restart(router, rail.job_id)["job_id"]
    router.CLOSE_GRACE_SECONDS = 0.1
    router.handle_close({"job_id": child_id})
    assert child_session.exited is True
    _next_session(rail, "proc_grandchild")

    answer = _restart(router, child_id, task="a third go")

    assert answer["success"] is False
    assert "replacement" in answer["error"]


def test_restart_reuses_the_source_metadata_with_the_new_task(rail):
    router = rail.router
    router.handle_close({"job_id": rail.job_id})
    _next_session(rail)
    source = router._read_record(rail.job_id)

    answer = _restart(router, rail.job_id)
    child = router._read_record(answer["job_id"])

    for field in ("workdir", "model", "effort", "max_turns", "permission_mode", "repo_id"):
        assert child[field] == source[field], field
    # The requirements are the caller's new ones, and the lineage is visible.
    assert NEW_TASK in rail.calls[-1]["command"]
    assert "do the original work" not in rail.calls[-1]["command"]
    assert rail.job_id in Path(child["inbox_file"]).read_text(encoding="utf-8")


def test_restart_never_reopens_or_writes_to_the_closed_source_stream(rail):
    router = rail.router
    router.handle_close({"job_id": rail.job_id})
    assert rail.registry.stdin_closed is True
    _next_session(rail)

    _restart(router, rail.job_id)

    assert rail.registry.stdin_writes == []
    assert rail.session.exited is True


def test_restart_surfaces_the_lineage_in_status_and_list(rail):
    router = rail.router
    router.handle_close({"job_id": rail.job_id})
    _next_session(rail)
    child_id = _restart(router, rail.job_id)["job_id"]

    child_status = json.loads(router.handle_status({"job_id": child_id}))
    source_status = json.loads(router.handle_status({"job_id": rail.job_id}))
    listing = json.loads(router.handle_list({}))

    assert child_status["restart_of_job_id"] == rail.job_id
    assert source_status["restarted_by_job_id"] == child_id
    assert [row["job_id"] for row in listing["sessions"]] == [child_id]
    assert listing["sessions"][0]["restart_pending"] is False


# --------------------------------------------------------------------------
# 4. The fences the restart may not open
# --------------------------------------------------------------------------

def test_restart_keeps_an_authorized_recovery_a_single_chain(rail):
    """The replacement inherits the recovery lineage; it never mints a new one."""
    router = rail.router
    now = time.time()
    router._write_record({
        "job_id": "claude-fablefail", "model": "fable", "state": "failed",
        "started_at": now - 500, "completed_at": now - 100,
        "repo_id": router._repo_identity(rail.workdir), "workdir": str(rail.workdir),
    })
    source = router._read_record(rail.job_id)
    source["recovery_of_job_id"] = "claude-fablefail"
    router._write_record(source)
    router.handle_close({"job_id": rail.job_id})
    _next_session(rail)

    answer = _restart(router, rail.job_id)

    assert answer["success"] is True, answer.get("error")
    child = router._read_record(answer["job_id"])
    assert child["recovery_of_job_id"] == "claude-fablefail"
    assert child["restart_of_job_id"] == rail.job_id


def test_restart_is_blocked_by_a_live_parallel_parent_in_the_same_turn(rail, tmp_path):
    """A replacement is an ordinary parent: it obeys the same-turn rules."""
    router = rail.router
    router.handle_close({"job_id": rail.job_id})
    other = tmp_path / "other-checkout"
    other.mkdir()
    router._write_record({
        "job_id": "claude-lane1", "state": "running", "model": "opus",
        "started_at": time.time(), "workdir": str(other),
        "repo_id": router._repo_identity(other), "origin_task_id": TURN,
        "parallel_lane": "analysis",
    })
    _next_session(rail)

    answer = _restart(router, rail.job_id)

    assert answer["success"] is False
    assert "claude-lane1" in answer["error"]


def test_restart_tool_is_registered_and_declared(router):
    registered: list[str] = []

    class Ctx:
        def register_tool(self, **kwargs):
            registered.append(kwargs["name"])

        def register_hook(self, *_args, **_kwargs):
            return None

    router.register(Ctx())

    assert "claude_code_restart" in registered
    manifest = (Path(router.PLUGIN_DIR) / "plugin.yaml").read_text(encoding="utf-8")
    assert "claude_code_restart" in manifest
