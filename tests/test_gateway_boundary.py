"""The plugin exactly as the live gateway runs it: real loader, real rail.

The v1.6.1 exit reconciler passed its unit tests and its standalone
real-registry test, and still did nothing for job ``claude-bbb95a9ee9a7``.
The gateway's own transcript (state.db message 202012, the
``claude_code_close`` answer at 18:20:27Z) shows why: the gateway answers
with the plugin module it imported at startup (15:02Z), and edits made to the
plugin after that moment are invisible until the gateway restarts or force
reloads. Nothing in the tool answers or in the durable record said *which*
code was answering, so the live proof tested code that was not running and
had no way to notice.

Everything here goes through the objects the gateway uses:

* ``hermes_cli.plugins.PluginManager._load_plugin`` imports the plugin as
  ``hermes_plugins.claude_auto_router`` and calls ``register(ctx)``;
* ``tools.registry.registry.dispatch`` invokes the handlers with the gateway's
  ``task_id``/``session_id`` keyword arguments;
* the real ``terminal_tool`` background launch, the real ``ProcessRegistry``
  and a real child process stand in for the Claude pipe.

Skipped (not failed) when the Hermes agent tree is not importable.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import sys
import time
from pathlib import Path

import pytest

HERMES_AGENT = Path(
    os.environ.get("HERMES_AGENT_DIR", Path.home() / ".hermes" / "hermes-agent")
)
PLUGIN_DIR = Path(__file__).parents[1]
MODULE_NAME = "hermes_plugins.claude_auto_router"
TERMINAL_FIELDS = ("state", "outcome", "completed_at", "completion_signal", "terminal_report")

pytestmark = pytest.mark.skipif(
    not (HERMES_AGENT / "hermes_cli" / "plugins.py").is_file()
    or not (HERMES_AGENT / "tools" / "process_registry.py").is_file(),
    reason="Hermes agent tree not available",
)

# Mirrors the Claude launch pipe: drain stdin to EOF, keep working briefly so
# claude_code_close genuinely observes a live process inside its grace window,
# then report the final turn and exit cleanly.
FAKE_CLAUDE = """#!/bin/sh
cat > /dev/null
sleep 1
printf '%s\\n' '{"type":"result","subtype":"success","num_turns":1,"duration_ms":5}'
exit 0
"""


def _manifest_version(plugin_dir: Path) -> str:
    text = (plugin_dir / "plugin.yaml").read_text(encoding="utf-8")
    match = re.search(r"^version:\s*['\"]?([^'\"\s#]+)", text, re.MULTILINE)
    assert match, "plugin.yaml has no version"
    return match.group(1)


def _load_like_the_gateway(hp, manager, plugin_dir: Path):
    """Import + register one directory plugin through the gateway's loader."""
    manifest = manager._parse_manifest(plugin_dir / "plugin.yaml", plugin_dir, "user", "")
    assert manifest is not None, "plugin.yaml did not parse"
    manager._load_plugin(manifest)
    loaded = manager._plugins[manifest.key or manifest.name]
    assert loaded.error is None, loaded.error
    module = loaded.module
    assert module.__name__ == MODULE_NAME
    assert sys.modules[MODULE_NAME] is module
    return manifest, module


class GatewayRail:
    """One gateway-loaded plugin instance plus the registries it talks to."""

    def __init__(self, *, hp, manager, module, registry, process_registry, workdir: Path):
        self.hp = hp
        self.manager = manager
        self.module = module
        self.registry = registry
        self.process_registry = process_registry
        self.workdir = workdir
        self.scope = manager.scope_key

    def dispatch(self, name: str, args: dict) -> dict:
        """Exactly how the gateway's tool executor reaches a plugin handler."""
        raw = self.registry.dispatch(
            name, args, scope=self.scope, task_id="turn-1", session_id="sess-parent"
        )
        return json.loads(raw)

    def handler_for(self, name: str):
        entry = self.registry.get_entry(name, scope=self.scope)
        return entry.handler if entry is not None else None


@pytest.fixture
def gateway(tmp_path, monkeypatch):
    if str(HERMES_AGENT) not in sys.path:
        sys.path.insert(0, str(HERMES_AGENT))
    import tools.process_registry as pr
    from hermes_cli import plugins as hp
    from tools.registry import registry

    monkeypatch.setattr(pr, "CHECKPOINT_PATH", tmp_path / "processes.json")
    process_registry = pr.ProcessRegistry()
    monkeypatch.setattr(pr, "process_registry", process_registry)

    manager = hp.PluginManager()
    _, module = _load_like_the_gateway(hp, manager, PLUGIN_DIR)

    # The registry must hand the gateway *this* module's handlers, not a
    # leftover from an earlier load in the same process.
    entry = registry.get_entry("claude_code_close", scope=manager.scope_key)
    assert entry is not None and entry.handler is module.handle_close

    runs = tmp_path / "runs"
    runs.mkdir()
    module.RUNS_DIR = runs
    fake_claude = tmp_path / "claude"
    fake_claude.write_text(FAKE_CLAUDE, encoding="utf-8")
    fake_claude.chmod(0o755)
    module.CLAUDE = str(fake_claude)
    workdir = tmp_path / "repo"
    workdir.mkdir()

    rail = GatewayRail(
        hp=hp, manager=manager, module=module, registry=registry,
        process_registry=process_registry, workdir=workdir,
    )
    yield rail
    for session_id in list(process_registry._running):
        try:
            process_registry.kill_process(session_id, consume_output=False)
        except Exception:
            pass
    try:
        manager.unload()
    finally:
        hp._clear_plugin_submodules(manager)


def _await_terminal(module, job_id: str, timeout: float = 30.0) -> dict:
    """Wait for the on-disk record to go terminal by itself (no status poll)."""
    worker = module._RECONCILERS.get(job_id)
    if worker is not None:
        worker.join(timeout)
    deadline = time.monotonic() + timeout
    while True:
        record = module._read_record(job_id) or {}
        if record.get("state") in module.TERMINAL_STATES or time.monotonic() > deadline:
            return record
        time.sleep(0.05)


def _launch(gateway: GatewayRail) -> dict:
    launched = gateway.dispatch(
        "claude_code_dispatch", {"task": "do the work", "workdir": str(gateway.workdir)}
    )
    assert launched.get("success") is True, launched
    return launched


# --------------------------------------------------------------------------
# 1. The boundary that was missed: which code is answering?
# --------------------------------------------------------------------------

def test_every_answer_and_the_durable_record_identify_the_loaded_code(gateway):
    """A live proof must be able to tell which plugin code it is talking to.

    Job claude-bbb95a9ee9a7 was closed by a module imported at 15:02Z although
    the reconciler had been on disk since 17:14Z, and nothing in the close
    answer or the record exposed that. Every tool answer and every record
    write therefore carries the version and load time of the module that
    produced it, so a stale gateway is caught by the first response instead
    of by a 20-second wait on a record no running code was going to touch.
    """
    version = _manifest_version(PLUGIN_DIR)

    launched = _launch(gateway)
    assert launched.get("plugin_version") == version, launched
    loaded_at = launched.get("plugin_loaded_at")
    assert isinstance(loaded_at, str) and loaded_at.endswith("Z")
    job_id = launched["job_id"]

    record = gateway.module._read_record(job_id)
    assert record.get("plugin_version") == version
    assert record.get("plugin_loaded_at") == loaded_at

    closed = gateway.dispatch("claude_code_close", {"job_id": job_id})
    assert closed["state"] == "completed_unverified", closed
    assert closed.get("plugin_version") == version
    assert closed.get("plugin_loaded_at") == loaded_at

    settled = _await_terminal(gateway.module, job_id)
    assert settled["state"] == "completed_unverified", settled.get("state")
    # The terminal write names the code that made it, too.
    assert settled["completion_signal"].get("plugin_version") == version

    status = gateway.dispatch("claude_code_status", {"job_id": job_id})
    assert status.get("plugin_version") == version
    assert status.get("plugin_loaded_at") == loaded_at


def test_edits_after_load_are_invisible_until_the_plugin_is_reloaded(tmp_path, monkeypatch):
    """Deterministic reproduction of the live failure, and of its remedy.

    The gateway imports a plugin once per process. A job that edits the
    plugin (as claude-bbb95a9ee9a7 did) is still supervised by the module
    that was imported before the edit; only a reload (what ``discover_plugins
    (force=True)`` does: unload, then import again) or a gateway restart makes
    the new code answer.
    """
    if str(HERMES_AGENT) not in sys.path:
        sys.path.insert(0, str(HERMES_AGENT))
    from hermes_cli import plugins as hp
    from tools.registry import registry

    # A private copy so the real plugin source is never edited by a test.
    copy = tmp_path / "claude-auto-router"
    copy.mkdir()
    for name in ("__init__.py", "plugin.yaml"):
        shutil.copy(PLUGIN_DIR / name, copy / name)
    fake_claude = tmp_path / "claude"
    fake_claude.write_text(FAKE_CLAUDE, encoding="utf-8")
    fake_claude.chmod(0o755)
    workdir = tmp_path / "repo"
    workdir.mkdir()
    loaded_version = _manifest_version(copy)

    manager = hp.PluginManager()
    try:
        manifest, module = _load_like_the_gateway(hp, manager, copy)
        module.CLAUDE = str(fake_claude)

        def probe() -> dict:
            raw = registry.dispatch(
                "claude_code_dispatch",
                {"task": "probe", "workdir": str(workdir), "dry_run": True},
                scope=manager.scope_key, task_id="turn-1", session_id="sess-parent",
            )
            return json.loads(raw)

        assert probe().get("plugin_version") == loaded_version

        # The "job" now edits the plugin on disk: a newer version appears.
        manifest_text = (copy / "plugin.yaml").read_text(encoding="utf-8")
        edited_version = f"{loaded_version}-edited"
        (copy / "plugin.yaml").write_text(
            re.sub(r"^version:.*$", f"version: {edited_version}", manifest_text, flags=re.MULTILINE),
            encoding="utf-8",
        )
        assert _manifest_version(copy) == edited_version

        # Same process, no reload: the gateway keeps answering with the code
        # it imported, whatever the files on disk now say.
        assert probe().get("plugin_version") == loaded_version
        assert sys.modules[MODULE_NAME] is module

        # The remedy: unload + import again, exactly what a force reload or a
        # gateway restart does. Only now does the edited code answer.
        manager.unload()
        _, reloaded = _load_like_the_gateway(hp, manager, copy)
        assert reloaded is not module
        reloaded.CLAUDE = str(fake_claude)
        assert probe().get("plugin_version") == edited_version
    finally:
        try:
            manager.unload()
        finally:
            hp._clear_plugin_submodules(manager)


# --------------------------------------------------------------------------
# 2. Convergence through the gateway-loaded module, real rail, real exit
# --------------------------------------------------------------------------

def test_close_then_real_exit_converges_through_the_gateway_loaded_module(gateway):
    """close -> EOF -> real exit -> terminal record, with no status poll.

    Same proof as the standalone real-registry test, but the module is the
    one the gateway loader produced and every call goes through the tool
    registry with the gateway's keyword arguments.
    """
    launched = _launch(gateway)
    job_id = launched["job_id"]
    session_id = launched["process_session_id"]
    session = gateway.process_registry.get(session_id)
    assert session is not None and not session.exited

    # The exit watcher is a daemon thread owned by the gateway-loaded module.
    worker = gateway.module._RECONCILERS.get(job_id)
    assert worker is not None and worker.daemon and worker.is_alive()
    assert worker.name == f"claude-reconcile:{job_id}"

    # The child takes the EOF and ends its own turn inside the close grace, so
    # the close records that exit rather than forcing a termination.
    closed = gateway.dispatch("claude_code_close", {"job_id": job_id})
    assert closed["state"] == "completed_unverified" and closed["terminal"] is True, closed
    assert closed["close_termination"]["forced"] is False

    # _move_to_finished set this when it enqueued the single completion
    # notification the gateway will deliver.
    assert session._completion_event.wait(20), "child did not exit"
    assert session.exited and session.exit_code == 0

    record = _await_terminal(gateway.module, job_id)
    missing = [field for field in TERMINAL_FIELDS if not record.get(field)]
    assert not missing, f"terminal record is missing {missing}: {record.get('state')!r}"
    assert record["state"] == "completed_unverified"
    assert record["completion_signal"]["kind"] == "terminal_state"
    assert record["completion_signal"]["outcome"] == "completed_unverified"
    assert record["terminal_report"]["exit_code"] == 0
    assert record["terminal_report"]["result_event_seen"] is True
    assert not record.get("requires_explicit_recovery")

    # Exactly one core completion event, and the plugin consumed nothing:
    # no wait()/read_log() (would mark it consumed) and no poll() after exit
    # (would mark it observed) — so no claude_code_status happened either.
    completions = []
    while not gateway.process_registry.completion_queue.empty():
        event = gateway.process_registry.completion_queue.get_nowait()
        if event.get("type") == "completion" and event.get("session_id") == session_id:
            completions.append(event)
    assert len(completions) == 1, completions
    assert gateway.process_registry.is_completion_consumed(session_id) is False
    assert session_id not in gateway.process_registry._poll_observed

    status_text = Path(record["status_file"]).read_text(encoding="utf-8")
    assert status_text.count("## Terminal outcome (Hermes supervisor)") == 1
