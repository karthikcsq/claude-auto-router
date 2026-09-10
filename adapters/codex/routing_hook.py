#!/usr/bin/env python3
"""Codex prompt-augmentation hook for Claude Auto Router."""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional, Set


POLICY_MARKER = "[CLAUDE_DELEGATION_ROUTING_POLICY_V1]"
FALLBACK_MARKER = "[CLAUDE_DELEGATION_ROUTING_FALLBACK_V1]"
SUPPORTED_EVENTS = {"UserPromptSubmit", "SubagentStart"}
DEFAULT_CONFIG = Path(__file__).with_name("config.json")


class HookConfigurationError(RuntimeError):
    pass


def load_config(path: Optional[Path] = None) -> Dict[str, Any]:
    selected = path or Path(os.environ.get("CLAUDE_DELEGATION_HOOK_CONFIG", str(DEFAULT_CONFIG)))
    try:
        value = json.loads(selected.expanduser().read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise HookConfigurationError("unable to read Claude delegation hook configuration") from exc
    if not isinstance(value, dict):
        raise HookConfigurationError("Claude delegation hook configuration must be an object")
    return value


def agent_identity(event: Dict[str, Any]) -> Optional[str]:
    event_name = event.get("hook_event_name")
    if event_name == "UserPromptSubmit":
        return "coordinator"
    if event_name == "SubagentStart":
        value = event.get("agent_type")
        return value if isinstance(value, str) and value else None
    return None


def is_targeted(event: Dict[str, Any], config: Dict[str, Any]) -> bool:
    if not config.get("enabled", True) or event.get("hook_event_name") not in SUPPORTED_EVENTS:
        return False
    identity = agent_identity(event)
    targets = config.get("target_agents", ["coordinator"])
    return isinstance(targets, list) and identity in targets


def _normalized_tool_names(values: Iterable[Any]) -> Set[str]:
    names: Set[str] = set()
    for value in values:
        if isinstance(value, str):
            names.add(value)
            names.add(value.rsplit("__", 1)[-1])
            names.add(value.rsplit(".", 1)[-1])
        elif isinstance(value, dict) and isinstance(value.get("name"), str):
            names.update(_normalized_tool_names([value["name"]]))
    return names


def _event_tools(event: Dict[str, Any]) -> Optional[Set[str]]:
    for key in ("available_tools", "tools", "tool_names"):
        values = event.get(key)
        if isinstance(values, list):
            return _normalized_tool_names(values)
    return None


def _registered_server_is_expected(config: Dict[str, Any]) -> bool:
    codex_bin = str(config.get("codex_bin", "codex"))
    server_name = str(config.get("mcp_server", "claude_delegation"))
    expected_server = str(Path(str(config["server_path"])).expanduser().resolve())
    try:
        result = subprocess.run(
            [codex_bin, "mcp", "get", server_name],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=3,
            check=False,
            shell=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return result.returncode == 0 and "enabled: true" in result.stdout and expected_server in result.stdout


def _server_tool_list(config: Dict[str, Any]) -> Set[str]:
    python_bin = str(config.get("python_bin", "/usr/bin/python3"))
    server_path = str(Path(str(config["server_path"])).expanduser().resolve())
    roots = config.get("allowed_roots", [])
    if not isinstance(roots, list) or not roots:
        return set()
    environment = {
        "HOME": str(Path.home()),
        "PATH": os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin"),
        "CLAUDE_DELEGATION_ALLOWED_ROOTS": os.pathsep.join(str(Path(root).expanduser().resolve()) for root in roots),
        "CLAUDE_DELEGATION_STATE_DIR": str(Path(str(config["state_dir"])).expanduser().resolve()),
    }
    claude_cli = config.get("claude_cli")
    if isinstance(claude_cli, str) and claude_cli:
        environment["CLAUDE_DELEGATION_CLI"] = claude_cli
    requests = (
        json.dumps(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {"protocolVersion": "2025-06-18", "capabilities": {}},
            }
        )
        + "\n"
        + json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"})
        + "\n"
        + json.dumps({"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}})
        + "\n"
    )
    try:
        result = subprocess.run(
            [python_bin, server_path],
            input=requests,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=3,
            check=False,
            shell=False,
            env=environment,
        )
    except (OSError, subprocess.TimeoutExpired):
        return set()
    if result.returncode != 0:
        return set()
    for line in reversed(result.stdout.splitlines()):
        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            continue
        tools = payload.get("result", {}).get("tools") if isinstance(payload, dict) else None
        if isinstance(tools, list):
            return _normalized_tool_names(tools)
    return set()


def claude_tools_available(event: Dict[str, Any], config: Dict[str, Any]) -> bool:
    required = set(config.get("required_tools", []))
    if not required:
        return False
    visible = _event_tools(event)
    if visible is not None:
        return required.issubset(visible)
    if not _registered_server_is_expected(config):
        return False
    return required.issubset(_server_tool_list(config))


def load_policy(config: Dict[str, Any]) -> str:
    inline = config.get("policy_text")
    if isinstance(inline, str) and inline.strip():
        policy = inline.strip()
    else:
        policy_path = Path(str(config.get("policy_file", ""))).expanduser()
        try:
            policy = policy_path.read_text(encoding="utf-8").strip()
        except OSError as exc:
            raise HookConfigurationError("unable to read Claude delegation routing policy") from exc
    if POLICY_MARKER not in policy:
        policy = POLICY_MARKER + "\n" + policy
    return policy


def _dedupe_key(event: Dict[str, Any], identity: str) -> str:
    turn_id = event.get("turn_id")
    if isinstance(turn_id, str) and turn_id:
        raw = str(event.get("session_id", "")) + "\0" + turn_id + "\0" + identity
    else:
        raw = (
            str(event.get("session_id", ""))
            + "\0"
            + str(event.get("hook_event_name", ""))
            + "\0"
            + identity
            + "\0"
            + str(event.get("prompt", ""))
        )
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _claim_once(event: Dict[str, Any], config: Dict[str, Any]) -> bool:
    identity = agent_identity(event)
    if identity is None:
        return False
    state_root = Path(str(config.get("state_dir", "~/.local/state/claude-delegation-hook"))).expanduser().resolve()
    marker_dir = state_root / "routing-hook-invocations"
    marker_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(marker_dir, 0o700)
    ttl_seconds = max(1, int(config.get("dedupe_ttl_hours", 168))) * 3600
    cutoff = time.time() - ttl_seconds
    for candidate in marker_dir.iterdir():
        try:
            if candidate.is_file() and candidate.stat().st_mtime < cutoff:
                candidate.unlink()
        except OSError:
            pass
    marker = marker_dir / _dedupe_key(event, identity)
    try:
        descriptor = os.open(str(marker), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError:
        return False
    try:
        os.write(descriptor, (str(event.get("hook_event_name", "")) + "\n").encode("utf-8"))
    finally:
        os.close(descriptor)
    return True


def build_response(
    event: Dict[str, Any],
    config: Dict[str, Any],
    availability_check: Callable[[Dict[str, Any], Dict[str, Any]], bool] = claude_tools_available,
    claim_once: Callable[[Dict[str, Any], Dict[str, Any]], bool] = _claim_once,
) -> Dict[str, Any]:
    if not is_targeted(event, config):
        return {}
    prompt = event.get("prompt")
    if isinstance(prompt, str) and (POLICY_MARKER in prompt or FALLBACK_MARKER in prompt):
        return {}
    if not claim_once(event, config):
        return {}
    if availability_check(event, config):
        context = load_policy(config)
    elif config.get("fallback_behavior", "inject_note") == "inject_note":
        context = str(config.get("fallback_text", FALLBACK_MARKER + " Claude delegation tools are unavailable."))
        if FALLBACK_MARKER not in context:
            context = FALLBACK_MARKER + " " + context
    else:
        return {}
    event_name = str(event["hook_event_name"])
    return {
        "hookSpecificOutput": {
            "hookEventName": event_name,
            "additionalContext": context,
        }
    }


def main() -> int:
    try:
        if len(sys.argv) > 2:
            raise HookConfigurationError("expected at most one configuration path")
        config_path = Path(sys.argv[1]) if len(sys.argv) == 2 else None
        event = json.load(sys.stdin)
        if not isinstance(event, dict):
            raise HookConfigurationError("hook input must be a JSON object")
        response = build_response(event, load_config(config_path))
        sys.stdout.write(json.dumps(response, sort_keys=True) + "\n")
        return 0
    except (HookConfigurationError, json.JSONDecodeError, OSError, ValueError):
        # Fail open without echoing hook input or configuration values.
        sys.stdout.write("{}\n")
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
