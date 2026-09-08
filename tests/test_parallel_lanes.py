"""Opt-in same-turn parallel Claude parents, and the compact active-session list.

The default is unchanged and must stay unchanged: a second live parent in the
same coordinator turn is blocked. Parallelism is an explicit two-sided opt-in
(`parallel_lane` on *both* jobs), bounded to two live parents per turn, and
only over checkouts that cannot collide — never for Fable and never for a
recovery job.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import time
from pathlib import Path

import pytest

from conftest import FakeRegistry, FakeSession, install_fake_rail

LAUNCH_OK = {"session_id": "proc_fake000000", "pid": 4242}
RESULT_OK = '{"type":"result","subtype":"success","num_turns":4,"duration_ms":900}\n'
TURN = "turn-42"

requires_git = pytest.mark.skipif(shutil.which("git") is None, reason="git not available")


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------

def _dir(base: Path, *parts: str) -> Path:
    path = base.joinpath(*parts)
    path.mkdir(parents=True, exist_ok=True)
    return path


def _git_repo(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q"], cwd=path, check=True, capture_output=True)
    subprocess.run(
        ["git", "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-q",
         "--allow-empty", "-m", "root"],
        cwd=path, check=True, capture_output=True,
    )
    return path


def _live(router, *, job_id: str, workdir: Path, lane: str | None, **updates) -> dict:
    record: dict = {
        "job_id": job_id,
        "state": "running",
        "model": "opus",
        "started_at": time.time(),
        "workdir": str(workdir.resolve()),
        "repo_id": router._repo_identity(workdir),
        "origin_task_id": TURN,
        "parallel_lane": lane,
    }
    record.update(updates)
    router._write_record(record)
    return record


def _guard(router, *, workdir: Path, lane: str | None, task_id: str = TURN,
           recovery_of_job_id: str = "") -> str | None:
    return router._dispatch_guard(
        origin_task_id=task_id,
        repo_id=router._repo_identity(workdir),
        recovery_of_job_id=recovery_of_job_id,
        parallel_lane=lane or "",
        workdir=workdir.resolve(),
    )


def _dispatch(router, workdir: Path, **args) -> dict:
    payload = {"task": "do the work", "workdir": str(workdir), "dry_run": True}
    payload.update(args)
    return json.loads(router.handle_dispatch(payload, task_id=TURN, session_id="sess-parent"))


# --------------------------------------------------------------------------
# 1. The default is untouched
# --------------------------------------------------------------------------

def test_second_same_turn_parent_without_a_lane_is_still_blocked(router, tmp_path):
    _live(router, job_id="claude-first", workdir=_dir(tmp_path, "a"), lane=None)

    blocked = _guard(router, workdir=_dir(tmp_path, "b"), lane=None)

    assert blocked and "One parent job per turn" in blocked


def test_lane_opt_in_does_not_unblock_a_parent_that_never_opted_in(router, tmp_path):
    """Both sides must opt in: a laneless live parent still owns the turn."""
    _live(router, job_id="claude-first", workdir=_dir(tmp_path, "a"), lane=None)

    blocked = _guard(router, workdir=_dir(tmp_path, "b"), lane="analysis")

    assert blocked
    assert "claude-first" in blocked
    assert "parallel_lane" in blocked


def test_a_new_coordinator_turn_is_unaffected_by_lanes(router, tmp_path):
    _live(router, job_id="claude-first", workdir=_dir(tmp_path, "a"), lane="analysis")

    assert _guard(router, workdir=_dir(tmp_path, "b"), lane=None, task_id="other-turn") is None


# --------------------------------------------------------------------------
# 2. The opt-in path
# --------------------------------------------------------------------------

@requires_git
def test_two_distinct_lanes_over_separate_checkouts_are_allowed(router, tmp_path):
    first = _git_repo(tmp_path / "checkout-a")
    second = _git_repo(tmp_path / "checkout-b")
    _live(router, job_id="claude-first", workdir=first, lane="analysis")

    assert _guard(router, workdir=second, lane="implementation") is None


def test_duplicate_lane_is_rejected(router, tmp_path):
    _live(router, job_id="claude-first", workdir=_dir(tmp_path, "a"), lane="analysis")

    blocked = _guard(router, workdir=_dir(tmp_path, "b"), lane="analysis")

    assert blocked
    assert "analysis" in blocked and "claude-first" in blocked


def test_third_same_turn_parent_exceeds_the_cap_of_two(router, tmp_path):
    _live(router, job_id="claude-first", workdir=_dir(tmp_path, "a"), lane="analysis")
    _live(router, job_id="claude-second", workdir=_dir(tmp_path, "b"), lane="implementation")

    blocked = _guard(router, workdir=_dir(tmp_path, "c"), lane="review")

    assert blocked
    assert router.MAX_PARALLEL_PARENTS_PER_TURN == 2
    for token in ("claude-first", "claude-second", "analysis", "implementation"):
        assert token in blocked


def test_cap_counts_only_live_same_turn_parents(router, tmp_path):
    _live(router, job_id="claude-first", workdir=_dir(tmp_path, "a"), lane="analysis")
    _live(router, job_id="claude-done", workdir=_dir(tmp_path, "b"), lane="implementation",
          state="completed_unverified")

    assert _guard(router, workdir=_dir(tmp_path, "c"), lane="implementation") is None


@pytest.mark.parametrize("lane", ["", "  ", "Analysis", "lane with space", "a", "x" * 33, "../etc"])
def test_invalid_lane_identifiers_are_rejected(router, tmp_path, lane):
    response = _dispatch(router, _dir(tmp_path, "a"), parallel_lane=lane)

    assert response["success"] is False
    assert "parallel_lane" in response["error"]


# --------------------------------------------------------------------------
# 3. Two parallel parents may never share a checkout
# --------------------------------------------------------------------------

def test_identical_workdir_is_rejected(router, tmp_path):
    shared = _dir(tmp_path, "repo")
    _live(router, job_id="claude-first", workdir=shared, lane="analysis")

    blocked = _guard(router, workdir=shared, lane="implementation")

    assert blocked and "isolated" in blocked
    assert "claude-first" in blocked


@pytest.mark.parametrize("nested_first", [True, False])
def test_nested_workdirs_are_rejected(router, tmp_path, nested_first):
    outer = _dir(tmp_path, "repo")
    inner = _dir(tmp_path, "repo", "packages", "app")
    live, incoming = (inner, outer) if nested_first else (outer, inner)
    _live(router, job_id="claude-first", workdir=live, lane="analysis")

    blocked = _guard(router, workdir=incoming, lane="implementation")

    assert blocked and "isolated" in blocked


@requires_git
def test_linked_worktrees_of_one_repository_are_rejected(router, tmp_path):
    """Different paths, one git common directory: still one repository."""
    main = _git_repo(tmp_path / "main")
    linked = tmp_path / "linked"
    subprocess.run(
        ["git", "worktree", "add", "-q", "-b", "lane", str(linked)],
        cwd=main, check=True, capture_output=True,
    )
    _live(router, job_id="claude-first", workdir=main, lane="analysis")

    blocked = _guard(router, workdir=linked, lane="implementation")

    assert blocked and "isolated" in blocked


# --------------------------------------------------------------------------
# 4. No parallel Fable, no parallel recovery
# --------------------------------------------------------------------------

def test_fable_rejects_a_parallel_lane(router, tmp_path):
    response = _dispatch(router, _dir(tmp_path, "a"), model="fable", parallel_lane="analysis")

    assert response["success"] is False
    assert "fable" in response["error"].lower()


def test_recovery_rejects_a_parallel_lane(router, tmp_path):
    workdir = _dir(tmp_path, "a")
    router._write_record({
        "job_id": "claude-fable", "model": "fable", "state": "failed",
        "completed_at": time.time(), "repo_id": router._repo_identity(workdir),
        "started_at": time.time(),
    })

    response = _dispatch(
        router, workdir, parallel_lane="implementation", recovery_of_job_id="claude-fable"
    )

    assert response["success"] is False
    assert "recovery" in response["error"].lower()


def test_a_live_fable_parent_still_blocks_the_turn_outright(router, tmp_path):
    _live(router, job_id="claude-fable", workdir=_dir(tmp_path, "a"), lane=None, model="fable")

    blocked = _guard(router, workdir=_dir(tmp_path, "b"), lane="analysis")

    assert blocked and "claude-fable" in blocked


# --------------------------------------------------------------------------
# 5. The lane is carried by the response, the record and the status
# --------------------------------------------------------------------------

def test_dispatch_record_and_status_carry_the_lane(router, workdir, monkeypatch):
    install_fake_rail(monkeypatch, FakeRegistry(FakeSession()), LAUNCH_OK)
    response = json.loads(router.handle_dispatch(
        {"task": "do the work", "workdir": str(workdir), "parallel_lane": "implementation"},
        task_id=TURN, session_id="sess-parent",
    ))

    assert response["success"] is True
    assert response["parallel_lane"] == "implementation"
    assert router._read_record(response["job_id"])["parallel_lane"] == "implementation"
    status = json.loads(router.handle_status({"job_id": response["job_id"]}))
    assert status["parallel_lane"] == "implementation"


def test_status_of_a_job_dispatched_without_a_lane_stays_compatible(
    router, workdir, monkeypatch
):
    install_fake_rail(monkeypatch, FakeRegistry(FakeSession()), LAUNCH_OK)
    response = json.loads(router.handle_dispatch(
        {"task": "do the work", "workdir": str(workdir)}, task_id=TURN,
    ))

    assert response["parallel_lane"] is None
    status = json.loads(router.handle_status({"job_id": response["job_id"]}))
    assert status["parallel_lane"] is None


def test_status_of_a_legacy_record_without_the_field_is_readable(router, tmp_path):
    router._write_record({
        "job_id": "claude-legacy", "state": "running", "model": "opus",
        "started_at": time.time(), "workdir": str(tmp_path),
    })

    status = json.loads(router.handle_status({"job_id": "claude-legacy"}))

    assert status["success"] is True
    assert status["parallel_lane"] is None


# --------------------------------------------------------------------------
# 6. claude_code_list
# --------------------------------------------------------------------------

LIST_ROW_KEYS = {
    "job_id", "state", "model", "workdir", "parallel_lane", "started_at", "action_required",
    "restart_pending",
}


def _list(router) -> dict:
    return json.loads(router.handle_list({}))


def test_list_returns_only_active_sessions_in_deterministic_order(router, tmp_path):
    _live(router, job_id="claude-ccc", workdir=_dir(tmp_path, "c"), lane=None, started_at=300.0)
    _live(router, job_id="claude-aaa", workdir=_dir(tmp_path, "a"), lane="analysis",
          started_at=100.0)
    _live(router, job_id="claude-bbb", workdir=_dir(tmp_path, "b"), lane="implementation",
          started_at=100.0)

    listing = _list(router)

    assert listing["success"] is True
    assert listing["count"] == 3
    assert [row["job_id"] for row in listing["sessions"]] == [
        "claude-aaa", "claude-bbb", "claude-ccc"
    ]
    assert [row["parallel_lane"] for row in listing["sessions"]] == [
        "analysis", "implementation", None
    ]


def test_list_rows_are_compact_and_carry_no_transcript_or_secret(router, tmp_path):
    _live(
        router, job_id="claude-aaa", workdir=_dir(tmp_path, "a"), lane="analysis",
        command=["claude", "--dangerously-skip-permissions"],
        claude_result={"result": "SECRET-TOKEN"},
        terminal_report={"evidence": "SECRET-TOKEN"},
        inbox_file=str(tmp_path / "inbox.md"),
    )

    row = _list(router)["sessions"][0]

    assert set(row) == LIST_ROW_KEYS
    assert "SECRET-TOKEN" not in json.dumps(row)
    assert row["model"] == "opus"
    assert row["workdir"] == str((tmp_path / "a").resolve())
    assert row["action_required"] is False


def test_list_reports_action_required_sessions(router, tmp_path):
    _live(
        router, job_id="claude-aaa", workdir=_dir(tmp_path, "a"), lane=None,
        state="action_required",
        action_required={"summary": "Claude needs approval to run the suite"},
    )

    row = _list(router)["sessions"][0]

    assert row["state"] == "action_required"
    assert row["action_required"] is True


@pytest.mark.parametrize("state", sorted({"completed_unverified", "failed_terminal", "closed_by_user"}))
def test_list_excludes_terminal_sessions(router, tmp_path, state):
    _live(router, job_id="claude-live", workdir=_dir(tmp_path, "a"), lane=None)
    _live(router, job_id="claude-done", workdir=_dir(tmp_path, "b"), lane=None, state=state)

    listing = _list(router)

    assert [row["job_id"] for row in listing["sessions"]] == ["claude-live"]


def test_list_is_empty_when_nothing_is_active(router, tmp_path):
    _live(router, job_id="claude-done", workdir=_dir(tmp_path, "a"), lane=None,
          state="completed_unverified")

    listing = _list(router)

    assert listing["success"] is True
    assert listing["sessions"] == []
    assert listing["count"] == 0


def test_list_reconciles_a_dead_session_without_consuming_its_completion(
    router, workdir, monkeypatch
):
    session = FakeSession()
    registry = FakeRegistry(session)
    install_fake_rail(monkeypatch, registry, LAUNCH_OK)
    response = json.loads(router.handle_dispatch(
        {"task": "do the work", "workdir": str(workdir)}, task_id=TURN,
    ))
    job_id = response["job_id"]
    session.finish(exit_code=0, output=RESULT_OK)
    polls_before = registry.polls

    listing = _list(router)

    assert listing["sessions"] == []
    assert router._read_record(job_id)["state"] in router.TERMINAL_STATES
    # poll()/wait()/read_log() are the three consuming reads: taking any of them
    # would make the core drop the job's single completion notification.
    assert registry.polls == polls_before


def test_list_leaves_a_live_session_live_and_arms_its_reconciler(
    router, workdir, monkeypatch
):
    session = FakeSession()
    install_fake_rail(monkeypatch, FakeRegistry(session), LAUNCH_OK)
    response = json.loads(router.handle_dispatch(
        {"task": "do the work", "workdir": str(workdir)}, task_id=TURN,
    ))

    listing = _list(router)

    assert [row["job_id"] for row in listing["sessions"]] == [response["job_id"]]
    assert response["job_id"] in router._RECONCILERS


def test_list_tool_is_registered_and_declared(router):
    registered: list[str] = []

    class Ctx:
        def register_tool(self, **kwargs):
            registered.append(kwargs["name"])

        def register_hook(self, *_args, **_kwargs):
            return None

    router.register(Ctx())

    assert "claude_code_list" in registered
    manifest = (Path(router.PLUGIN_DIR) / "plugin.yaml").read_text(encoding="utf-8")
    assert "claude_code_list" in manifest
