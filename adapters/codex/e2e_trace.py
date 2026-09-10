#!/usr/bin/env python3
"""No-network fixture for the complete Codex to Claude delegation lifecycle."""

import importlib.util
import json
import subprocess
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

try:
    from adapters.codex.claude_code_delegation import DelegationManager, run_worker, tool_definitions
except ModuleNotFoundError:  # Direct script execution from a repository clone.
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
    from adapters.codex.claude_code_delegation import DelegationManager, run_worker, tool_definitions


HOOK = Path(__file__).with_name("routing_hook.py")


class FixtureProcess:
    pid = 4242


class FixtureCompletionTransport:
    def __init__(self, trace):
        self.trace = trace
        self.calls = []

    def __call__(self, event):
        self.calls.append(event)
        self.trace.append("terminal event resumes originating coordinator once")
        return {
            "accepted": True,
            "thread_id": event["callback_target"]["thread_id"],
            "turn_id": "fixture-resume-turn",
        }


def _load_hook():
    spec = importlib.util.spec_from_file_location("claude_routing_hook", HOOK)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def run_trace():
    trace = ["incoming engineering request"]
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        allowed = root / "allowed"
        workdir = allowed / "project"
        state = root / "state"
        workdir.mkdir(parents=True)
        (workdir / "verify.py").write_text(
            "from pathlib import Path\nassert Path('implemented.txt').read_text() == 'done\\n'\n",
            encoding="utf-8",
        )
        subprocess.run(["git", "init", "-q", str(workdir)], check=True, shell=False)
        subprocess.run(["git", "-C", str(workdir), "add", "verify.py"], check=True, shell=False)
        subprocess.run(
            [
                "git",
                "-C",
                str(workdir),
                "-c",
                "user.name=Fixture",
                "-c",
                "user.email=fixture@example.invalid",
                "commit",
                "-qm",
                "fixture baseline",
            ],
            check=True,
            shell=False,
        )

        hook = _load_hook()
        policy = root / "policy.md"
        policy.write_text(HOOK.with_name("routing-policy.md").read_text(encoding="utf-8"), encoding="utf-8")
        required = [item["name"] for item in tool_definitions()]
        hook_config = {
            "enabled": True,
            "target_agents": ["coordinator"],
            "policy_file": str(policy),
            "policy_text": None,
            "fallback_behavior": "inject_note",
            "required_tools": required,
            "state_dir": str(state),
            "dedupe_ttl_hours": 1,
        }
        hook_event = {
            "hook_event_name": "UserPromptSubmit",
            "session_id": "fixture-thread",
            "turn_id": "fixture-origin-turn",
            "prompt": "Implement a heavy multi-file change",
            "available_tools": required,
        }
        first = hook.build_response(hook_event, hook_config)
        second = hook.build_response(hook_event, hook_config)
        context = first["hookSpecificOutput"]["additionalContext"]
        assert context.count(hook.POLICY_MARKER) == 1 and second == {}
        trace.append("pre-LLM routing hook injects policy once")
        assert required == [
            "claude_code_dispatch",
            "claude_code_status",
            "claude_code_message",
            "claude_code_close",
            "claude_code_list",
            "claude_code_restart",
        ]
        trace.append("coordinator sees six delegation tools")
        selected_model = "opus"
        trace.append("heavy-task classification selects Claude Opus")

        fake_claude = root / "fake-claude"
        fake_claude.write_text(
            "#!/usr/bin/python3\n"
            "import json, pathlib, sys\n"
            "if sys.argv[1:] == ['auth', 'status']: raise SystemExit(0)\n"
            "sys.stdin.read()\n"
            "pathlib.Path('implemented.txt').write_text('done\\n')\n"
            "print(json.dumps({'session_id':'fixture-claude-session','result':'fixture validation passed'}))\n",
            encoding="utf-8",
        )
        fake_claude.chmod(0o700)
        transport = FixtureCompletionTransport(trace)
        manager = DelegationManager(
            state,
            [allowed],
            claude_bin=str(fake_claude),
            completion_transport=transport,
        )
        origin = {
            "thread_id": "fixture-thread",
            "session_id": "fixture-thread",
            "turn_id": "fixture-origin-turn",
            "coordinator_identity": "coordinator",
            "mcp_call_id": "fixture-dispatch-call",
        }
        baseline = manager._repo_snapshot(workdir)
        with patch.object(manager, "_validate_runtime", return_value=str(fake_claude)), patch.object(
            manager, "_repo_snapshot", return_value=baseline
        ), patch("adapters.codex.claude_code_delegation.subprocess.Popen", return_value=FixtureProcess()):
            dispatched = manager.dispatch(
                "Implement the fixture and satisfy verify.py",
                str(workdir),
                model=selected_model,
                callback_context=origin,
            )
        assert dispatched["callback_registered"]
        trace.append("dispatch returns job ID and durable callback registration")
        exit_code = run_worker(
            dispatched["job_id"],
            str(state),
            str(fake_claude),
            [str(allowed)],
            completion_transport=transport,
        )
        assert exit_code == 0 and len(transport.calls) == 1
        record = manager._read_job(dispatched["job_id"])
        assert record["status"] == "completed"
        assert record["completion_event"]["delivery_status"] == "delivered"
        assert "implemented.txt" in record["completion_event"]["payload"]["changed_files_or_components"]
        trace.insert(trace.index("terminal event resumes originating coordinator once"), "Claude worker completes")

        subprocess.run(["/usr/bin/python3", "verify.py"], cwd=str(workdir), check=True, shell=False)
        assert (workdir / "implemented.txt").read_text(encoding="utf-8") == "done\n"
        trace.append("coordinator independently verifies repository and tests")
        trace.append("coordinator reports verified outcome")
        closed = manager.close(dispatched["job_id"])
        assert closed["closed"] and closed["resources_released"]
        trace.append("coordinator calls claude_code_close")

        assert trace.index("coordinator independently verifies repository and tests") < trace.index(
            "coordinator reports verified outcome"
        )
        return dispatched["job_id"], trace


if __name__ == "__main__":
    job_id, trace = run_trace()
    print("fixture_job_id=" + job_id)
    for index, item in enumerate(trace, 1):
        print(str(index) + ". " + item)
