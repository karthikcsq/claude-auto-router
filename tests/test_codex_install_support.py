import json
import plistlib
from pathlib import Path

from adapters.codex.install_support import (
    hook_config,
    launch_agent_config,
    update_hooks,
    write_launch_agent,
)


def test_launch_agent_is_machine_specific_and_wake_safe(tmp_path: Path):
    target = tmp_path / "callback.plist"
    config = launch_agent_config(
        server="/repo/adapters/codex/claude_code_delegation.py",
        allowed_root="/projects",
        state_dir="/state",
        claude_bin="/tools/claude",
        codex_bin="/tools/codex",
    )
    write_launch_agent(target, config)
    with target.open("rb") as stream:
        installed = plistlib.load(stream)
    assert installed["ProgramArguments"][1] == "/repo/adapters/codex/claude_code_delegation.py"
    assert installed["RunAtLoad"] is True
    assert installed["StartCalendarInterval"] == {"Second": 0}
    assert target.stat().st_mode & 0o777 == 0o600


def test_hook_config_requires_all_six_codex_tools():
    config = hook_config(
        server="/repo/server.py",
        policy="/repo/policy.md",
        allowed_root="/projects",
        state_dir="/state",
        claude_bin="/tools/claude",
        codex_bin="/tools/codex",
    )
    assert config["server_path"] == "/repo/server.py"
    assert config["required_tools"] == [
        "claude_code_dispatch",
        "claude_code_status",
        "claude_code_message",
        "claude_code_close",
        "claude_code_list",
        "claude_code_restart",
    ]


def test_hook_merge_preserves_unrelated_hooks_and_replaces_old_adapter(tmp_path: Path):
    target = tmp_path / "hooks.json"
    target.write_text(
        json.dumps(
            {
                "hooks": {
                    "UserPromptSubmit": [
                        {"hooks": [{"command": "/other/hook.py"}]},
                        {
                            "hooks": [
                                {
                                    "command": (
                                        "/usr/bin/python3 /Users/me/.codex/hooks/"
                                        "claude-delegation-routing/pre_llm_call.py"
                                    )
                                }
                            ]
                        },
                    ]
                }
            }
        ),
        encoding="utf-8",
    )
    update_hooks(target, "/usr/bin/python3 /repo/adapters/codex/routing_hook.py /config.json")
    installed = json.loads(target.read_text(encoding="utf-8"))
    serialized = json.dumps(installed)
    assert "/other/hook.py" in serialized
    assert "claude-delegation-routing/pre_llm_call.py" not in serialized
    assert serialized.count("adapters/codex/routing_hook.py") == 2
    assert target.stat().st_mode & 0o777 == 0o600
