"""Regression tests for automatic Claude-router fallback/fan-out guards."""

from __future__ import annotations

import importlib.util
import time
from pathlib import Path

import pytest


PLUGIN = Path(__file__).parents[1] / "__init__.py"
SPEC = importlib.util.spec_from_file_location("claude_auto_router_test", PLUGIN)
assert SPEC and SPEC.loader
router = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(router)

NOW = 2_000_000_000.0


def _record(*, job_id: str, **updates: object) -> dict[str, object]:
    result: dict[str, object] = {
        "job_id": job_id,
        "state": "running",
        "started_at": time.time(),
        "model": "opus",
        "repo_id": "repo-a",
    }
    result.update(updates)
    return result


def _failed_fable(*, job_id: str, completed_at: float, **updates: object) -> dict[str, object]:
    return _record(
        job_id=job_id,
        model="fable",
        state="failed",
        completed_at=completed_at,
        **updates,
    )


def _write_reported_chain(tmp_path: Path) -> None:
    router.RUNS_DIR = tmp_path
    router._write_record(_failed_fable(
        job_id="claude-661d5a79a62e", completed_at=NOW - 300,
    ))
    router._write_record(_failed_fable(
        job_id="claude-17c863f903ad",
        completed_at=NOW - 200,
        recovery_of_job_id="claude-661d5a79a62e",
    ))
    router._write_record(_failed_fable(
        job_id="claude-63dc2d212a66",
        completed_at=NOW - 100,
        recovery_of_job_id="claude-17c863f903ad",
    ))


def test_terminal_success_with_background_wait_ceiling_is_incomplete_recoverable() -> None:
    outcome = router._classify_terminal_outcome(
        "Background tasks still running after 600s; terminating. "
        "7 spawned, 0 completed, 7 system-killed. No code/docs changes or PRs.",
        {"type": "result", "subtype": "success"},
    )

    assert outcome == "incomplete_recoverable"


def test_fable_command_disables_claude_background_wait_ceiling() -> None:
    command = router._command("fable", "max", 140, "auto", Path("/status"))

    assert command[:2] == ["env", "CLAUDE_CODE_PRINT_BG_WAIT_CEILING_MS=0"]


def test_blocks_second_dispatch_in_same_coordinator_turn(tmp_path: Path) -> None:
    router.RUNS_DIR = tmp_path
    router._write_record(_record(job_id="claude-first", origin_task_id="turn-42"))

    blocked = router._dispatch_guard(
        origin_task_id="turn-42", repo_id="repo-a", recovery_of_job_id=""
    )

    assert blocked and "One parent job per turn" in blocked


def test_allows_new_dispatch_after_prior_parent_is_closed(tmp_path: Path) -> None:
    router.RUNS_DIR = tmp_path
    router._write_record(_record(
        job_id="claude-finished", origin_task_id="turn-42", state="closing"
    ))

    assert router._dispatch_guard(
        origin_task_id="turn-42", repo_id="repo-a", recovery_of_job_id=""
    ) is None


def test_blocks_automatic_fable_to_opus_fallback(tmp_path: Path) -> None:
    router.RUNS_DIR = tmp_path
    router._write_record(_record(
        job_id="claude-fable", model="fable", state="failed", completed_at=time.time()
    ))

    blocked = router._dispatch_guard(
        origin_task_id="later-turn", repo_id="repo-a", recovery_of_job_id=""
    )

    assert blocked and "Do not automatically retry, downgrade, or fan out" in blocked


def test_allows_one_named_recovery_of_the_failed_fable_job(tmp_path: Path) -> None:
    router.RUNS_DIR = tmp_path
    router._write_record(_record(
        job_id="claude-fable", model="fable", state="failed", completed_at=time.time()
    ))

    assert router._dispatch_guard(
        origin_task_id="recovery-turn", repo_id="repo-a", recovery_of_job_id="claude-fable"
    ) is None


def test_allows_named_recovery_of_latest_failed_fable_chain_head(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(router.time, "time", lambda: NOW)
    _write_reported_chain(tmp_path)

    assert router._dispatch_guard(
        origin_task_id="recovery-turn",
        repo_id="repo-a",
        recovery_of_job_id="claude-63dc2d212a66",
    ) is None


def test_recovery_chain_rejects_missing_job_id(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(router.time, "time", lambda: NOW)
    _write_reported_chain(tmp_path)

    blocked = router._dispatch_guard(
        origin_task_id="recovery-turn", repo_id="repo-a", recovery_of_job_id=""
    )

    assert blocked and "Do not automatically retry, downgrade, or fan out" in blocked


@pytest.mark.parametrize(
    "recovery_of_job_id",
    ["claude-661d5a79a62e", "claude-17c863f903ad"],
)
def test_recovery_chain_rejects_non_latest_ancestor_ids(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    recovery_of_job_id: str,
) -> None:
    monkeypatch.setattr(router.time, "time", lambda: NOW)
    _write_reported_chain(tmp_path)

    blocked = router._dispatch_guard(
        origin_task_id="recovery-turn",
        repo_id="repo-a",
        recovery_of_job_id=recovery_of_job_id,
    )

    assert blocked and "latest failed Fable job" in blocked


def test_rejects_explicit_recovery_of_a_failure_outside_the_24h_fence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(router.time, "time", lambda: NOW)
    router.RUNS_DIR = tmp_path
    router._write_record(_failed_fable(
        job_id="claude-stale",
        completed_at=NOW - router.RECOVERY_FENCE_SECONDS - 1,
    ))

    blocked = router._dispatch_guard(
        origin_task_id="recovery-turn",
        repo_id="repo-a",
        recovery_of_job_id="claude-stale",
    )

    assert blocked and "not a recent failed Fable job in this repository" in blocked


def test_rejects_recovery_id_from_an_unrelated_repository(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(router.time, "time", lambda: NOW)
    router.RUNS_DIR = tmp_path
    router._write_record(_failed_fable(
        job_id="claude-repo-a", completed_at=NOW - 100, repo_id="repo-a",
    ))
    router._write_record(_failed_fable(
        job_id="claude-repo-b", completed_at=NOW - 50, repo_id="repo-b",
    ))

    blocked = router._dispatch_guard(
        origin_task_id="recovery-turn",
        repo_id="repo-a",
        recovery_of_job_id="claude-repo-b",
    )

    assert blocked and "latest failed Fable job" in blocked


def test_rejects_alternate_recovery_after_chain_head_already_has_a_child(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(router.time, "time", lambda: NOW)
    _write_reported_chain(tmp_path)
    router._write_record(_record(
        job_id="claude-existing-recovery",
        model="opus",
        state="failed_terminal",
        completed_at=NOW - 50,
        origin_task_id="prior-recovery-turn",
        recovery_of_job_id="claude-63dc2d212a66",
    ))

    blocked = router._dispatch_guard(
        origin_task_id="another-recovery-turn",
        repo_id="repo-a",
        recovery_of_job_id="claude-63dc2d212a66",
    )

    assert blocked and "already has recovery job claude-existing-recovery" in blocked


def test_handle_dispatch_refuses_automatic_fallback_before_cli_launch(tmp_path: Path) -> None:
    router.RUNS_DIR = tmp_path / "runs"
    repo_id = str(tmp_path.resolve())
    router._write_record(_record(
        job_id="claude-fable", model="fable", state="failed", repo_id=repo_id, completed_at=time.time()
    ))

    response = router.json.loads(router.handle_dispatch(
        {"task": "replace the failed campaign", "workdir": str(tmp_path), "model": "opus", "dry_run": True},
        task_id="later-turn",
    ))

    assert response["success"] is False
    assert "Do not automatically retry, downgrade, or fan out" in response["error"]


def test_expired_fable_failure_does_not_block_unrelated_future_work(tmp_path: Path) -> None:
    router.RUNS_DIR = tmp_path
    router._write_record(_record(
        job_id="claude-old-fable",
        model="fable",
        state="failed",
        completed_at=time.time() - router.RECOVERY_FENCE_SECONDS - 1,
    ))

    assert router._dispatch_guard(
        origin_task_id="future-turn", repo_id="repo-a", recovery_of_job_id=""
    ) is None
