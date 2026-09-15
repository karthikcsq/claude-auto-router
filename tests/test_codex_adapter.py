import io
import json
import os
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from adapters.codex.claude_code_delegation import (
    CallbackTransientError,
    CallbackUnavailable,
    CodexAppServerTransport,
    DelegationManager,
    SecretRedactor,
    ToolError,
    callback_context_from_mcp_meta,
    parse_allowed_roots,
    run_worker,
    serve,
    tool_definitions,
    utc_now,
)
from adapters.codex.install_support import launch_agent_config


class RecordingTransport:
    def __init__(self, failure=None, trace=None):
        self.calls = []
        self.failure = failure
        self.trace = trace

    def __call__(self, event):
        self.calls.append(event)
        if self.trace is not None:
            self.trace.append("terminal event resumes originating coordinator once")
        if self.failure:
            raise self.failure("delivery failed")
        return {"accepted": True, "thread_id": event["callback_target"]["thread_id"], "turn_id": "turn-resumed"}


class DelegationTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.base = Path(self.temporary.name)
        self.allowed = self.base / "allowed"
        self.allowed.mkdir()
        self.workdir = self.allowed / "project"
        self.workdir.mkdir()
        self.state = self.base / "state"

    def tearDown(self):
        self.temporary.cleanup()

    def manager(self, **overrides):
        options = {
            "state_dir": self.state,
            "allowed_roots": [self.allowed],
            "claude_bin": "claude",
        }
        options.update(overrides)
        return DelegationManager(**options)

    @staticmethod
    def callback(thread_id="thread-original"):
        return {
            "thread_id": thread_id,
            "session_id": thread_id,
            "turn_id": "turn-origin",
            "coordinator_identity": "coordinator",
            "mcp_call_id": "call-dispatch",
        }

    @patch("adapters.codex.claude_code_delegation.shutil.which", return_value="/usr/bin/claude")
    @patch("adapters.codex.claude_code_delegation.subprocess.run")
    def test_dry_run_authenticates_without_state_or_prompt_in_argv(self, run, _which):
        run.return_value = subprocess.CompletedProcess([], 0)
        result = self.manager().dispatch("secret task body", str(self.workdir), dry_run=True)
        self.assertTrue(result["dry_run"])
        self.assertEqual(result["prompt_transport"], "stdin")
        self.assertNotIn("secret task body", " ".join(result["command"]))
        self.assertFalse(self.state.exists())
        self.assertEqual(run.call_args.args[0], ["/usr/bin/claude", "auth", "status"])
        self.assertFalse(run.call_args.kwargs["shell"])

    @patch("adapters.codex.claude_code_delegation.DelegationManager._worker_alive", return_value=True)
    @patch("adapters.codex.claude_code_delegation.shutil.which", return_value="/usr/bin/claude")
    @patch("adapters.codex.claude_code_delegation.subprocess.Popen")
    @patch("adapters.codex.claude_code_delegation.subprocess.run")
    def test_dispatch_persists_secure_state_and_argv_worker(self, run, popen, _which, _worker_alive):
        run.return_value = subprocess.CompletedProcess([], 0)
        popen.side_effect = [Mock(pid=4321), Mock(pid=9876)]
        result = self.manager().dispatch(
            "Add endpoint",
            str(self.workdir),
            permission_mode="acceptEdits",
            effort="high",
            callback_context=self.callback(),
        )
        status_result = self.manager().status(result["job_id"])
        self.assertEqual(status_result["status"], "running")
        self.assertEqual(status_result["pid"], 4321)
        self.assertEqual(status_result["sleep_guard_status"], "armed")
        self.assertEqual(status_result["sleep_guard_pid"], 9876)
        self.assertNotIn("task", status_result)
        job_file = self.state / result["job_id"] / "job.json"
        self.assertEqual(stat.S_IMODE(job_file.stat().st_mode), 0o600)
        argv = popen.call_args_list[0].args[0]
        self.assertIsInstance(argv, list)
        self.assertFalse(popen.call_args_list[0].kwargs["shell"])
        self.assertNotIn("Add endpoint", " ".join(argv))
        self.assertEqual(
            popen.call_args_list[1].args[0],
            ["/usr/bin/caffeinate", "-i", "-w", "4321"],
        )
        self.assertFalse(popen.call_args_list[1].kwargs["shell"])
        self.assertTrue(result["callback_registered"])
        record = self.manager()._read_job(result["job_id"])
        self.assertEqual(record["callback"]["origin"]["thread_id"], "thread-original")
        self.assertEqual(record["callback"]["target"]["thread_id"], "thread-original")

    @patch("adapters.codex.claude_code_delegation.shutil.which", return_value="/usr/bin/claude")
    @patch("adapters.codex.claude_code_delegation.subprocess.Popen")
    @patch("adapters.codex.claude_code_delegation.subprocess.run")
    def test_missing_caffeinate_does_not_block_dispatch(self, run, popen, _which):
        run.return_value = subprocess.CompletedProcess([], 0)
        popen.return_value.pid = 4321
        result = self.manager(caffeinate_bin=str(self.base / "missing-caffeinate")).dispatch(
            "Add endpoint",
            str(self.workdir),
            callback_context=self.callback(),
        )
        self.assertEqual(result["sleep_guard_status"], "unavailable")
        self.assertEqual(len(popen.call_args_list), 1)

    def test_allowed_roots_are_explicit_and_state_is_outside_them(self):
        with self.assertRaises(ToolError):
            parse_allowed_roots(None)
        with self.assertRaises(ToolError):
            parse_allowed_roots("")
        self.assertEqual(parse_allowed_roots(str(self.allowed)), [self.allowed.resolve()])
        with self.assertRaises(ToolError):
            DelegationManager(self.allowed / "state", [self.allowed])

    def test_workdir_resolution_blocks_outside_and_symlink_escape(self):
        outside = self.base / "outside"
        outside.mkdir()
        link = self.allowed / "escape"
        link.symlink_to(outside, target_is_directory=True)
        manager = self.manager()
        with self.assertRaises(ToolError):
            manager._validate_workdir(str(outside))
        with self.assertRaises(ToolError):
            manager._validate_workdir(str(link))

    def test_command_applies_permission_effort_and_resume_without_shell_text(self):
        command = self.manager()._command(
            "/path/claude", "opus", "high", 12, "plan", "session; touch /tmp/nope"
        )
        self.assertEqual(command[0], "/path/claude")
        self.assertIn("--permission-mode", command)
        self.assertIn("--effort", command)
        self.assertEqual(command[-2:], ["--resume", "session; touch /tmp/nope"])

    @patch("adapters.codex.claude_code_delegation.shutil.which", return_value="/usr/bin/claude")
    @patch("adapters.codex.claude_code_delegation.subprocess.run")
    def test_dispatch_defaults_to_rolling_opus_and_accepts_fable_alias_or_pin(self, run, _which):
        run.return_value = subprocess.CompletedProcess([], 0)
        rolling = self.manager().dispatch("Do work", str(self.workdir), dry_run=True)
        fable = self.manager().dispatch("Do work", str(self.workdir), model="fable", dry_run=True)
        pinned = self.manager().dispatch(
            "Do work", str(self.workdir), model="claude-fable-5", dry_run=True
        )
        pinned_51 = self.manager().dispatch(
            "Do work", str(self.workdir), model="claude-fable-5-1", dry_run=True
        )
        self.assertEqual(rolling["model"], "opus")
        self.assertIn("opus", rolling["command"])
        self.assertFalse(any(part.startswith("claude-opus-") for part in rolling["command"]))
        self.assertIn("fable", fable["command"])
        self.assertIn("claude-fable-5", pinned["command"])
        self.assertIn("claude-fable-5-1", pinned_51["command"])

    def test_secret_redactor_covers_environment_assignments_and_bearer_tokens(self):
        redact = SecretRedactor({"ANTHROPIC_API_KEY": "environment-secret"})
        text = redact(
            "environment-secret api_key=inline-secret Authorization: Bearer abc.def password=hunter2"
        )
        for secret in ("environment-secret", "inline-secret", "abc.def", "hunter2"):
            self.assertNotIn(secret, text)

    def _seed_job(self, job_id, **updates):
        manager = self.manager()
        record = {
            "job_id": job_id,
            "status": "running",
            "phase": "running",
            "objective": "complete delegated task",
            "task": "Do work",
            "workdir": str(self.workdir.resolve()),
            "allowed_roots": [str(self.allowed.resolve())],
            "claude_executable": "claude",
            "model": "opus",
            "effort": None,
            "max_turns": 40,
            "permission_mode": "default",
            "messages": [],
            "initial_completed": False,
            "close_requested": False,
            "created_at": "now",
            "updated_at": "now",
            "pid": os.getpid(),
            "worker_identity": None,
            "heartbeat_at": "now",
            "sleep_guard_pid": None,
            "sleep_guard_status": "disabled",
            "latest_validation": None,
            "blocker": None,
            "next_action": None,
            "session_id": None,
            "exit_code": None,
            "turn_count": 0,
            "repo_snapshot": {"kind": "non_git", "head": None, "paths": {}},
            "callback": None,
            "completion_event": None,
            "restart_of_job_id": None,
            "restarted_by_job_id": None,
        }
        record.update(updates)
        with manager._locked_job(job_id, create=True) as jobdir:
            manager._write_unlocked(jobdir, record)
        return manager

    def test_message_is_durable_private_and_terminal_rejects(self):
        manager = self._seed_job("claude-message")
        response = manager.message("claude-message", "Also add tests with token=secret-value")
        self.assertEqual(response["queued_messages"], 1)
        status_result = manager.status("claude-message")
        self.assertEqual(status_result["queued_messages"], 1)
        self.assertNotIn("messages", status_result)
        record = manager._read_job("claude-message")
        self.assertEqual(record["messages"][0]["state"], "queued")
        manager._update_job("claude-message", lambda item: item.update({"status": "completed"}))
        with self.assertRaises(ToolError):
            manager.message("claude-message", "too late")

    def test_status_reconciles_dead_worker(self):
        manager = self._seed_job("claude-dead", pid=99_999_999)
        status_result = manager.status("claude-dead")
        self.assertEqual(status_result["status"], "failed")
        self.assertIn("no longer exists", status_result["blocker"])

    def test_wake_recovery_keeps_a_live_worker_even_with_old_heartbeat(self):
        transport = RecordingTransport()
        manager = self._seed_job(
            "claude-live-after-sleep",
            pid=4321,
            worker_identity="claude-delegation-worker-v1",
            heartbeat_at="2000-01-01T00:00:00+00:00",
            callback={
                "origin": self.callback(),
                "target": {"type": "codex_app_server_application_context", "thread_id": "thread-original"},
            },
        )
        manager.completion_transport = transport
        with patch.object(manager, "_worker_alive", return_value=True):
            outcome = manager.recover_pending_completions()
        self.assertEqual(outcome["active"], 1)
        self.assertEqual(manager._read_job("claude-live-after-sleep")["status"], "running")
        self.assertEqual(transport.calls, [])

    def test_restart_recovery_terminalizes_a_lost_worker_once_without_replay(self):
        transport = RecordingTransport()
        manager = self._seed_job(
            "claude-lost-after-restart",
            pid=4321,
            worker_identity="claude-delegation-worker-v1",
            callback={
                "origin": self.callback(),
                "target": {"type": "codex_app_server_application_context", "thread_id": "thread-original"},
            },
        )
        manager.completion_transport = transport
        with patch.object(manager, "_worker_alive", return_value=False):
            first = manager.recover_pending_completions()
            second = manager.recover_pending_completions()
        record = manager._read_job("claude-lost-after-restart")
        self.assertEqual(first["recovered_failed"], 1)
        self.assertEqual(first["delivered"], 1)
        self.assertEqual(second["recovered_failed"], 0)
        self.assertEqual(record["status"], "failed")
        self.assertIn("sleep, logout, or restart", record["blocker"])
        self.assertEqual(len(transport.calls), 1)

    def test_recent_queued_job_gets_launch_race_grace(self):
        manager = self._seed_job(
            "claude-launch-race",
            status="queued",
            pid=None,
            created_at=utc_now(),
        )
        with patch.object(manager, "_worker_alive", return_value=False):
            outcome = manager.recover_pending_completions()
        self.assertEqual(outcome["active"], 1)
        self.assertEqual(manager._read_job("claude-launch-race")["status"], "queued")

    def test_launch_agent_has_wake_coalescing_calendar_schedule(self):
        config = launch_agent_config(
            server="/repo/adapters/codex/claude_code_delegation.py",
            allowed_root="/projects",
            state_dir="/state",
            claude_bin="/bin/claude",
            codex_bin="/bin/codex",
        )
        self.assertTrue(config["RunAtLoad"])
        self.assertEqual(config["StartCalendarInterval"], {"Second": 0})

    def test_close_is_graceful_and_idempotent(self):
        manager = self._seed_job("claude-close")
        first = manager.close("claude-close")
        self.assertTrue(first["close_requested"])
        self.assertFalse(first["closed"])
        manager._update_job("claude-close", lambda item: item.update({"status": "cancelled"}))
        second = manager.close("claude-close")
        self.assertTrue(second["closed"])
        self.assertEqual(second["status"], "cancelled")

    def test_list_returns_only_compact_active_jobs(self):
        manager = self._seed_job("claude-listed")
        self._seed_job("claude-finished", status="completed")
        with patch.object(manager, "_worker_alive", return_value=True):
            result = manager.list_active()
        self.assertEqual(result["count"], 1)
        self.assertEqual(result["active_jobs"][0]["job_id"], "claude-listed")
        self.assertNotIn("task", result["active_jobs"][0])

    def test_restart_replaces_only_one_user_closed_non_fable_job(self):
        manager = self._seed_job(
            "claude-restart-source",
            status="cancelled",
            close_requested=True,
        )
        with patch.object(
            manager,
            "dispatch",
            return_value={"job_id": "claude-replacement", "status": "running"},
        ) as dispatch:
            result = manager.restart(
                "claude-restart-source",
                "Continue with current requirements",
                callback_context=self.callback(),
            )
        self.assertEqual(result["job_id"], "claude-replacement")
        self.assertEqual(
            manager._read_job("claude-restart-source")["restarted_by_job_id"],
            "claude-replacement",
        )
        self.assertEqual(dispatch.call_args.kwargs["restart_of_job_id"], "claude-restart-source")
        with self.assertRaisesRegex(ToolError, "already has a replacement"):
            manager.restart(
                "claude-restart-source",
                "Try again",
                callback_context=self.callback(),
            )

    def test_restart_refuses_fable_and_failed_jobs(self):
        manager = self._seed_job(
            "claude-fable-closed",
            status="cancelled",
            close_requested=True,
            model="fable",
        )
        with self.assertRaisesRegex(ToolError, "Fable"):
            manager.restart(
                "claude-fable-closed",
                "Continue",
                callback_context=self.callback(),
            )
        self._seed_job("claude-failed-source", status="failed")
        with self.assertRaisesRegex(ToolError, "closed by the user"):
            manager.restart(
                "claude-failed-source",
                "Retry",
                callback_context=self.callback(),
            )

    def _fake_claude(self):
        fake = self.base / "fake-claude"
        fake.write_text(
            "#!/usr/bin/python3\n"
            "import json, pathlib, sys\n"
            "if sys.argv[1:] == ['auth', 'status']:\n"
            "    raise SystemExit(0)\n"
            "prompt = sys.stdin.read()\n"
            "pathlib.Path('invocations.txt').open('a').write(json.dumps({'argv': sys.argv[1:], 'prompt': prompt}) + '\\n')\n"
            "print(json.dumps({'session_id': 'safe-session', 'result': 'ok token=super-secret'}))\n",
            encoding="utf-8",
        )
        fake.chmod(0o700)
        return fake

    def test_worker_handles_followup_redacts_status_and_keeps_logs_metadata_only(self):
        fake = self._fake_claude()
        manager = self._seed_job(
            "claude-worker",
            messages=[
                {"id": "message-1", "message": "Follow up", "queued_at": "now", "state": "queued"}
            ],
        )
        result = run_worker("claude-worker", str(self.state), str(fake), [str(self.allowed)])
        self.assertEqual(result, 0)
        record = manager._read_job("claude-worker")
        self.assertEqual(record["status"], "completed")
        self.assertNotIn("super-secret", record["latest_validation"])
        self.assertEqual(record["messages"][0]["state"], "delivered")
        invocations = [json.loads(line) for line in (self.workdir / "invocations.txt").read_text().splitlines()]
        self.assertEqual(len(invocations), 2)
        self.assertNotIn("Do work", " ".join(invocations[0]["argv"]))
        self.assertIn("Follow-up requirement", invocations[1]["prompt"])
        self.assertEqual(invocations[1]["argv"][-2:], ["--resume", "safe-session"])
        all_logs = "".join(path.read_text() for path in (self.state / "claude-worker").glob("*.log"))
        self.assertNotIn("super-secret", all_logs)
        self.assertNotIn("Follow up", all_logs)
        stream_logs = list((self.state / "claude-worker").glob("claude-turn-*.log"))
        self.assertEqual(len(stream_logs), 4)
        self.assertTrue(all(stat.S_IMODE(path.stat().st_mode) == 0o600 for path in stream_logs))

    def test_worker_honors_close_before_start_without_launching_claude(self):
        fake = self._fake_claude()
        manager = self._seed_job(
            "claude-preclosed",
            close_requested=True,
            callback={
                "origin": self.callback(),
                "target": {"type": "codex_app_server_application_context", "thread_id": "thread-original"},
            },
        )
        transport = RecordingTransport()
        result = run_worker(
            "claude-preclosed",
            str(self.state),
            str(fake),
            [str(self.allowed)],
            completion_transport=transport,
        )
        self.assertEqual(result, 0)
        record = manager._read_job("claude-preclosed")
        self.assertEqual(record["status"], "cancelled")
        self.assertEqual(record["completion_event"]["payload"]["terminal_status"], "cancelled")
        self.assertEqual(len(transport.calls), 1)
        self.assertFalse((self.workdir / "invocations.txt").exists())

    def test_success_emits_and_delivers_one_durable_terminal_event(self):
        transport = RecordingTransport()
        manager = self._seed_job(
            "claude-success",
            status="completed",
            sleep_guard_status="armed",
            callback={
                "origin": self.callback(),
                "target": {"type": "codex_app_server_application_context", "thread_id": "thread-original"},
            },
        )
        manager.completion_transport = transport
        first = manager.emit_completion("claude-success")
        second = manager.emit_completion("claude-success")
        self.assertEqual(first["event_name"], "claude_delegation_completed")
        self.assertEqual(first["delivery_status"], "delivered")
        self.assertEqual(first["event_id"], second["event_id"])
        self.assertEqual(len(transport.calls), 1)
        self.assertEqual(manager._read_job("claude-success")["sleep_guard_status"], "released")
        event_lines = [
            json.loads(line)
            for line in (self.state / "claude-success" / "events.log").read_text().splitlines()
            if json.loads(line)["event"] == "claude_delegation_completed"
        ]
        self.assertEqual(len(event_lines), 1)

    def test_failure_and_cancellation_deliver_truthful_terminal_events(self):
        for suffix, status in (("failure", "failed"), ("cancel", "cancelled")):
            transport = RecordingTransport()
            manager = self._seed_job(
                "claude-" + suffix,
                status=status,
                model="fable" if status == "failed" else "opus",
                blocker="worker failed" if status == "failed" else None,
                callback={
                    "origin": self.callback(),
                    "target": {"type": "codex_app_server_application_context", "thread_id": "thread-original"},
                },
            )
            manager.completion_transport = transport
            completion = manager.emit_completion("claude-" + suffix)
            manager.emit_completion("claude-" + suffix)
            self.assertEqual(completion["payload"]["terminal_status"], status)
            self.assertEqual(len(transport.calls), 1)
            events = [
                json.loads(line)
                for line in (self.state / ("claude-" + suffix) / "events.log").read_text().splitlines()
            ]
            self.assertEqual(
                sum(item["event"] == "claude_delegation_completed" for item in events), 1
            )

    def test_duplicate_terminal_signals_resume_coordinator_once(self):
        transport = RecordingTransport()
        manager = self._seed_job(
            "claude-dedup",
            status="completed",
            callback={
                "origin": self.callback(),
                "target": {"type": "codex_app_server_application_context", "thread_id": "thread-original"},
            },
        )
        manager.completion_transport = transport
        for _ in range(3):
            manager.emit_completion("claude-dedup")
        self.assertEqual(len(transport.calls), 1)

    def test_restart_recovery_delivers_pending_event_once(self):
        transient = RecordingTransport(CallbackTransientError)
        manager = self._seed_job(
            "claude-restart",
            status="completed",
            callback={
                "origin": self.callback(),
                "target": {"type": "codex_app_server_application_context", "thread_id": "thread-original"},
            },
        )
        manager.completion_transport = transient
        self.assertEqual(manager.emit_completion("claude-restart")["delivery_status"], "pending")
        delivered = RecordingTransport()
        restarted = self.manager(completion_transport=delivered)
        outcome = restarted.recover_pending_completions()
        self.assertEqual(outcome["delivered"], 1)
        self.assertEqual(len(delivered.calls), 1)
        restarted.recover_pending_completions()
        self.assertEqual(len(delivered.calls), 1)

    def test_deleted_origin_is_unavailable_and_never_misrouted(self):
        transport = RecordingTransport(CallbackUnavailable)
        manager = self._seed_job(
            "claude-deleted",
            status="completed",
            callback={
                "origin": self.callback("thread-deleted"),
                "target": {"type": "codex_app_server_application_context", "thread_id": "thread-deleted"},
            },
        )
        manager.completion_transport = transport
        result = manager.emit_completion("claude-deleted")
        self.assertEqual(result["delivery_status"], "unavailable")
        self.assertEqual(transport.calls[0]["callback_target"]["thread_id"], "thread-deleted")

    def test_native_app_server_delivery_is_application_context_and_idempotent(self):
        rollout = self.base / "rollout.jsonl"
        requests = self.base / "requests.jsonl"
        fake_codex = self.base / "fake-codex"
        fake_codex.write_text(
            "#!/usr/bin/python3\n"
            "import json, os, pathlib, sys\n"
            "if sys.argv[1:] == ['app-server','daemon','start']: raise SystemExit(0)\n"
            "if sys.argv[1:] != ['app-server','proxy']: raise SystemExit(2)\n"
            "rollout=pathlib.Path(os.environ['FIXTURE_ROLLOUT'])\n"
            "requests=pathlib.Path(os.environ['FIXTURE_REQUESTS'])\n"
            "for line in sys.stdin:\n"
            " req=json.loads(line)\n"
            " requests.open('a').write(json.dumps(req)+'\\n')\n"
            " if 'id' not in req: continue\n"
            " method=req['method']\n"
            " if method=='initialize': result={'serverInfo':{'name':'fixture'}}\n"
            " elif method=='thread/read': result={'thread':{'id':'thread-exact','path':str(rollout)}}\n"
            " elif method=='thread/resume': result={'thread':{'id':'thread-exact','path':str(rollout)}}\n"
            " elif method=='turn/start':\n"
            "  rollout.open('a').write(json.dumps(req['params'])+'\\n'); result={'turn':{'id':'turn-callback'}}\n"
            " else: result={}\n"
            " print(json.dumps({'jsonrpc':'2.0','id':req['id'],'result':result}), flush=True)\n",
            encoding="utf-8",
        )
        fake_codex.chmod(0o700)
        event = {
            "idempotency_key": "cde-id",
            "event_marker": "[CLAUDE_DELEGATION_COMPLETION_EVENT:cde-id]",
            "resume_marker": "[CLAUDE_DELEGATION_COMPLETION_EVENT:cde-id:RESUME]",
            "callback_target": {"thread_id": "thread-exact"},
            "payload": {"job_id": "claude-native", "terminal_status": "completed"},
        }

        def marker_seen(path, marker):
            return bool(path) and Path(path).is_file() and marker in Path(path).read_text(encoding="utf-8")

        transport = CodexAppServerTransport(str(fake_codex), SecretRedactor({}), timeout=5)
        with patch.dict(
            os.environ,
            {"FIXTURE_ROLLOUT": str(rollout), "FIXTURE_REQUESTS": str(requests)},
        ), patch.object(CodexAppServerTransport, "_marker_seen", side_effect=marker_seen):
            first = transport(event)
            second = transport(event)
        self.assertFalse(first["already_present"])
        self.assertTrue(second["already_present"])
        sent = [json.loads(line) for line in requests.read_text().splitlines()]
        starts = [item for item in sent if item.get("method") == "turn/start"]
        self.assertEqual(len(starts), 1)
        params = starts[0]["params"]
        self.assertEqual(params["input"], [])
        self.assertEqual(
            params["additionalContext"]["claude_delegation_completed"]["kind"], "application"
        )
        self.assertEqual(params["toolOutput"]["name"], "claude_delegation_completed")
        self.assertEqual(params["toolOutput"]["namespace"], "claude_delegation")

    def test_fable_failure_has_no_retry_downgrade_or_fanout(self):
        fake = self.base / "failing-claude"
        fake.write_text(
            "#!/usr/bin/python3\n"
            "import pathlib, sys\n"
            "if sys.argv[1:] == ['auth', 'status']: raise SystemExit(0)\n"
            "pathlib.Path('fable-invocations.txt').open('a').write(' '.join(sys.argv[1:]) + '\\n')\n"
            "raise SystemExit(7)\n",
            encoding="utf-8",
        )
        fake.chmod(0o700)
        manager = self._seed_job(
            "claude-fable-fail",
            model="fable",
            callback={
                "origin": self.callback(),
                "target": {"type": "codex_app_server_application_context", "thread_id": "thread-original"},
            },
        )
        transport = RecordingTransport()
        result = run_worker(
            "claude-fable-fail",
            str(self.state),
            str(fake),
            [str(self.allowed)],
            completion_transport=transport,
        )
        self.assertEqual(result, 7)
        invocations = (self.workdir / "fable-invocations.txt").read_text().splitlines()
        self.assertEqual(len(invocations), 1)
        self.assertIn("fable", invocations[0])
        self.assertNotIn("opus", invocations[0])
        record = manager._read_job("claude-fable-fail")
        self.assertEqual(record["completion_event"]["payload"]["terminal_status"], "failed")
        self.assertEqual(record["completion_event"]["delivery_status"], "delivered")
        self.assertEqual(len(transport.calls), 1)

    def test_auth_failure_never_copies_cli_output(self):
        with patch("adapters.codex.claude_code_delegation.shutil.which", return_value="/usr/bin/claude"), patch(
            "adapters.codex.claude_code_delegation.subprocess.run"
        ) as run:
            run.return_value = subprocess.CompletedProcess([], 1, stdout=b"token=secret", stderr=b"password=secret")
            with self.assertRaisesRegex(ToolError, "not authenticated") as raised:
                self.manager()._validate_runtime()
            self.assertNotIn("secret", str(raised.exception))

    def test_mcp_lists_exactly_six_tools_and_returns_tool_errors_as_results(self):
        manager = self.manager()
        self.assertEqual(
            [tool["name"] for tool in tool_definitions()],
            [
                "claude_code_dispatch",
                "claude_code_status",
                "claude_code_message",
                "claude_code_close",
                "claude_code_list",
                "claude_code_restart",
            ],
        )
        requests = io.StringIO(
            json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
            + "\n"
            + json.dumps(
                {
                    "jsonrpc": "2.0",
                    "id": 2,
                    "method": "tools/call",
                    "params": {"name": "claude_code_status", "arguments": {"job_id": "bad"}},
                }
            )
            + "\n"
        )
        responses = io.StringIO()
        serve(manager, requests, responses)
        payloads = [json.loads(line) for line in responses.getvalue().splitlines()]
        self.assertEqual(len(payloads[0]["result"]["tools"]), 6)
        self.assertTrue(payloads[1]["result"]["isError"])

    def test_mcp_origin_metadata_binds_dispatch_to_exact_coordinator(self):
        context = callback_context_from_mcp_meta(
            {
                "threadId": "thread-exact",
                "callId": "call-1",
                "x-codex-turn-metadata": {
                    "session_id": "session-exact",
                    "turn_id": "turn-exact",
                },
            }
        )
        self.assertEqual(
            context,
            {
                "thread_id": "thread-exact",
                "session_id": "session-exact",
                "turn_id": "turn-exact",
                "coordinator_identity": "coordinator",
                "mcp_call_id": "call-1",
            },
        )


if __name__ == "__main__":
    unittest.main()
