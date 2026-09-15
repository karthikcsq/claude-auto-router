"""Shared fixtures: an isolated router module plus a fake Hermes process rail.

The plugin imports ``tools.terminal_tool`` / ``tools.process_registry`` lazily
inside its handlers, so tests can install fakes in ``sys.modules`` and drive the
whole dispatch/status lifecycle deterministically — no Claude CLI, no gateway.
"""

from __future__ import annotations

import importlib.util
import inspect
import json
import sys
import threading
import types
from pathlib import Path

import pytest

PLUGIN = Path(__file__).parents[1] / "__init__.py"


def spawn_writable(registry, command: str, *, cwd: str):
    """Start a writable real Hermes session across old and current rails."""
    parameters = inspect.signature(registry.spawn_local).parameters
    if "keep_stdin_open" in parameters:
        return registry.spawn_local(
            command, cwd=cwd, keep_stdin_open=True
        )
    return registry.spawn_local(command, cwd=cwd, use_pty=True)


def load_router(runs_dir: Path):
    """Fresh module instance so RUNS_DIR patching never leaks between tests."""
    spec = importlib.util.spec_from_file_location("claude_auto_router_under_test", PLUGIN)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module.RUNS_DIR = runs_dir
    module.CLAUDE = "/bin/sh"  # any real file: only an existence check gates launch
    return module


class FakeSession:
    """Mirrors the fields of tools.process_registry.ProcessSession we touch."""

    def __init__(self, session_id: str = "proc_fake000000", *, routed: bool = True):
        self.id = session_id
        self.command = "claude -p"
        self.session_key = "agent:main:discord:chat-1"
        self.watcher_platform = "discord" if routed else ""
        self.watcher_chat_id = "chat-1" if routed else ""
        self.watcher_user_id = "user-1" if routed else ""
        self.watcher_user_name = "karthik" if routed else ""
        self.watcher_thread_id = "thread-1" if routed else ""
        self.watcher_message_id = "msg-1" if routed else ""
        self.parent_session_id = "sess-parent" if routed else ""
        self.watcher_interval = 0
        self.notify_on_complete = False
        self.watch_patterns: list[str] = []
        self.exited = False
        self.exit_code = None
        self.completion_reason = "exited"
        self.termination_source = ""
        self.output_buffer = ""
        self._lock = threading.Lock()
        # Mirrors ProcessSession._completion_event: set exactly once by
        # _move_to_finished, which is also where the core enqueues the single
        # completion notification. Anything watching for a terminal transition
        # without consuming the notification must key off this.
        self._completion_event = threading.Event()

    def finish(self, *, exit_code: int = 0, reason: str = "exited", output: str | None = None):
        """Exactly what ProcessRegistry does when the child really exits."""
        if output is not None:
            self.output_buffer = output
        self.exited = True
        self.exit_code = exit_code
        self.completion_reason = reason
        self._completion_event.set()
        return self


class FakeRegistry:
    def __init__(self, session: FakeSession | None = None):
        self.session = session
        self.pending_watchers: list[dict] = []
        self.checkpoints = 0
        self.stdin_writes: list[str] = []
        self.stdin_closed = False
        # kill_process(consume_output=True) is what marks the core's single
        # completion notification as already delivered; anything that must not
        # steal the user's callback has to pass False.
        self.kills: list[dict] = []
        self.completion_consumed: set[str] = set()
        # poll() marks _poll_observed in the real registry, which suppresses
        # the CLI drain's completion injection. Counting polls lets a test
        # prove background reconciliation never takes that path.
        self.polls = 0

    # --- API surface used by the plugin -------------------------------------
    def get(self, session_id: str):
        if self.session is not None and session_id == self.session.id:
            return self.session
        return None

    def poll(self, session_id: str) -> dict:
        self.polls += 1
        session = self.get(session_id)
        if session is None:
            return {"status": "not_found", "error": f"No process with ID {session_id}"}
        result = {
            "session_id": session.id,
            "status": "exited" if session.exited else "running",
            "pid": 4242,
            "uptime_seconds": 7,
        }
        if session.exited:
            result["exit_code"] = session.exit_code
            result["completion_reason"] = session.completion_reason
            result["termination_source"] = session.termination_source
        return result

    def _write_checkpoint(self):
        self.checkpoints += 1

    def write_stdin(self, session_id: str, data: str = "") -> dict:
        """Raw write: records exactly the bytes the plugin chose to frame."""
        self.stdin_writes.append(data)
        return {"status": "ok"}

    def submit_stdin(self, session_id: str, data: str = "") -> dict:
        self.stdin_writes.append(data)
        return {"status": "ok"}

    def close_stdin(self, session_id: str) -> dict:
        session = self.get(session_id)
        if session is None:
            return {"status": "not_found", "error": f"No process with ID {session_id}"}
        if session.exited:
            return {"status": "already_exited", "error": "Process has already finished"}
        self.stdin_closed = True
        return {"status": "ok", "message": "stdin closed"}

    def kill_process(
        self, session_id: str, *, source: str = "process.kill", consume_output: bool = True
    ) -> dict:
        """Mirrors ProcessRegistry.kill_process, including its consumption rule."""
        self.kills.append(
            {"session_id": session_id, "source": source, "consume_output": consume_output}
        )
        session = self.get(session_id)
        if session is None:
            return {"status": "not_found", "error": f"No process with ID {session_id}"}
        if consume_output:
            self.completion_consumed.add(session_id)
        if session.exited:
            return {
                "status": "already_exited",
                "exit_code": session.exit_code,
                "completion_reason": session.completion_reason,
            }
        session.exited = True
        session.exit_code = -15
        session.completion_reason = "killed"
        session.termination_source = source
        # _move_to_finished sets this, and it is what wakes the exit watcher.
        session._completion_event.set()
        return {
            "status": "killed",
            "session_id": session.id,
            "completion_reason": "killed",
            "termination_source": source,
        }

    def is_completion_consumed(self, session_id: str) -> bool:
        return session_id in self.completion_consumed


def install_fake_rail(monkeypatch, registry: FakeRegistry, launch: dict) -> list[dict]:
    """Install fake tools.terminal_tool / tools.process_registry modules.

    Returns the list that records every terminal_tool(**kwargs) call, so a test
    can assert on the exact launch arguments.
    """
    calls: list[dict] = []

    def terminal_tool(**kwargs):
        calls.append(kwargs)
        return json.dumps(launch)

    tools_pkg = sys.modules.get("tools")
    if tools_pkg is None or not hasattr(tools_pkg, "__path__"):
        tools_pkg = types.ModuleType("tools")
        tools_pkg.__path__ = []  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "tools", tools_pkg)

    terminal_mod = types.ModuleType("tools.terminal_tool")
    terminal_mod.terminal_tool = terminal_tool
    monkeypatch.setitem(sys.modules, "tools.terminal_tool", terminal_mod)

    registry_mod = types.ModuleType("tools.process_registry")
    registry_mod.process_registry = registry
    monkeypatch.setitem(sys.modules, "tools.process_registry", registry_mod)
    return calls


def exit_during_grace(session: FakeSession, *, delay: float = 0.05, **finish):
    """Let the managed child end on its own inside the close grace window."""
    timer = threading.Timer(delay, lambda: session.finish(**finish))
    timer.daemon = True
    timer.start()
    return timer


@pytest.fixture
def runs_dir(tmp_path: Path) -> Path:
    path = tmp_path / "runs"
    path.mkdir()
    return path


@pytest.fixture
def router(runs_dir: Path):
    return load_router(runs_dir)


@pytest.fixture
def workdir(tmp_path: Path) -> Path:
    path = tmp_path / "repo"
    path.mkdir()
    return path
