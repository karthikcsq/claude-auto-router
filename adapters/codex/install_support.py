#!/usr/bin/env python3
"""Generate user-specific Codex files without committing machine paths."""

from __future__ import annotations

import argparse
import json
import os
import plistlib
import shlex
from pathlib import Path
from typing import Any


LABEL = "com.openai.codex.claude-delegation-callback"
HOOK_EVENTS = ("UserPromptSubmit", "SubagentStart")


def launch_agent_config(
    *, server: str, allowed_root: str, state_dir: str, claude_bin: str, codex_bin: str
) -> dict[str, Any]:
    return {
        "Label": LABEL,
        "ProgramArguments": [
            "/usr/bin/python3",
            server,
            "--recover-callbacks",
            "--allowed-root",
            allowed_root,
            "--state-dir",
            state_dir,
            "--claude-bin",
            claude_bin,
            "--codex-bin",
            codex_bin,
        ],
        "RunAtLoad": True,
        "StartInterval": 60,
        # launchd coalesces missed calendar events and fires once after wake.
        "StartCalendarInterval": {"Second": 0},
        "ProcessType": "Background",
        "StandardOutPath": "/dev/null",
        "StandardErrorPath": "/dev/null",
    }


def hook_config(
    *, server: str, policy: str, allowed_root: str, state_dir: str,
    claude_bin: str, codex_bin: str
) -> dict[str, Any]:
    return {
        "enabled": True,
        "target_agents": ["coordinator"],
        "policy_file": policy,
        "policy_text": None,
        "fallback_behavior": "inject_note",
        "fallback_text": (
            "[CLAUDE_DELEGATION_ROUTING_FALLBACK_V1] The Claude delegation tools are "
            "unavailable for this model call. Do not claim to dispatch or monitor Claude."
        ),
        "mcp_server": "claude_delegation",
        "required_tools": [
            "claude_code_dispatch",
            "claude_code_status",
            "claude_code_message",
            "claude_code_close",
            "claude_code_list",
            "claude_code_restart",
        ],
        "codex_bin": codex_bin,
        "claude_cli": claude_bin,
        "python_bin": "/usr/bin/python3",
        "server_path": server,
        "allowed_roots": [allowed_root],
        "state_dir": state_dir,
        "dedupe_ttl_hours": 168,
    }


def _atomic_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    with temporary.open("w", encoding="utf-8") as stream:
        json.dump(value, stream, indent=2, sort_keys=True)
        stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())
    os.chmod(temporary, 0o600)
    os.replace(temporary, path)


def write_launch_agent(path: Path, config: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    with temporary.open("wb") as stream:
        plistlib.dump(config, stream, fmt=plistlib.FMT_XML, sort_keys=False)
        stream.flush()
        os.fsync(stream.fileno())
    os.chmod(temporary, 0o600)
    os.replace(temporary, path)


def update_hooks(path: Path, hook_command: str) -> None:
    try:
        current = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        current = {}
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError("existing Codex hooks configuration is unreadable") from exc
    if not isinstance(current, dict):
        raise RuntimeError("existing Codex hooks configuration is not an object")
    hooks = current.setdefault("hooks", {})
    if not isinstance(hooks, dict):
        raise RuntimeError("existing Codex hooks field is not an object")
    entry = {
        "hooks": [
            {
                "type": "command",
                "command": hook_command,
                "timeout": 5,
                "statusMessage": "Applying Claude delegation routing policy",
                "additionalContextLimit": 16000,
            }
        ]
    }
    for event in HOOK_EVENTS:
        existing = hooks.get(event, [])
        if not isinstance(existing, list):
            raise RuntimeError(f"existing {event} hooks are not a list")
        retained = []
        for candidate in existing:
            serialized = json.dumps(candidate, sort_keys=True)
            if "claude-delegation-routing/pre_llm_call.py" in serialized:
                continue
            if "adapters/codex/routing_hook.py" in serialized:
                continue
            retained.append(candidate)
        hooks[event] = [*retained, entry]
    current.setdefault("description", "Codex hooks including Claude Auto Router.")
    _atomic_json(path, current)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--server", required=True)
    parser.add_argument("--routing-hook", required=True)
    parser.add_argument("--policy", required=True)
    parser.add_argument("--allowed-root", required=True)
    parser.add_argument("--state-dir", required=True)
    parser.add_argument("--claude-bin", required=True)
    parser.add_argument("--codex-bin", required=True)
    parser.add_argument("--launch-agent", required=True)
    parser.add_argument("--hook-config", required=True)
    parser.add_argument("--hooks-json", required=True)
    args = parser.parse_args()

    config = hook_config(
        server=args.server,
        policy=args.policy,
        allowed_root=args.allowed_root,
        state_dir=args.state_dir,
        claude_bin=args.claude_bin,
        codex_bin=args.codex_bin,
    )
    _atomic_json(Path(args.hook_config), config)
    write_launch_agent(
        Path(args.launch_agent),
        launch_agent_config(
            server=args.server,
            allowed_root=args.allowed_root,
            state_dir=args.state_dir,
            claude_bin=args.claude_bin,
            codex_bin=args.codex_bin,
        ),
    )
    command = " ".join(
        shlex.quote(part)
        for part in ("/usr/bin/python3", args.routing_hook, args.hook_config)
    )
    update_hooks(Path(args.hooks_json), command)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
