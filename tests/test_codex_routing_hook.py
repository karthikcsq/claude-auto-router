import copy
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from adapters.codex.routing_hook import (
    FALLBACK_MARKER,
    POLICY_MARKER,
    build_response,
    claude_tools_available,
    is_targeted,
)


REQUIRED_TOOLS = [
    "claude_code_dispatch",
    "claude_code_status",
    "claude_code_message",
    "claude_code_close",
    "claude_code_list",
    "claude_code_restart",
]


class RoutingHookTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.policy = self.root / "policy.md"
        self.policy.write_text(POLICY_MARKER + "\nRoute heavy work through Claude.\n", encoding="utf-8")
        self.config = {
            "enabled": True,
            "target_agents": ["coordinator"],
            "policy_file": str(self.policy),
            "policy_text": None,
            "fallback_behavior": "inject_note",
            "fallback_text": FALLBACK_MARKER + " Claude tools unavailable.",
            "required_tools": REQUIRED_TOOLS,
            "state_dir": str(self.root / "state"),
            "dedupe_ttl_hours": 1,
        }

    def tearDown(self):
        self.temporary.cleanup()

    @staticmethod
    def event(turn="turn-1", **updates):
        value = {
            "session_id": "session-1",
            "turn_id": turn,
            "cwd": "/workspace",
            "hook_event_name": "UserPromptSubmit",
            "prompt": "Implement the feature",
            "model": "gpt-test",
            "available_tools": REQUIRED_TOOLS,
        }
        value.update(updates)
        return value

    def test_injects_policy_before_qualifying_coordinator_call(self):
        response = build_response(self.event(), self.config)
        context = response["hookSpecificOutput"]["additionalContext"]
        self.assertIn(POLICY_MARKER, context)

    def test_does_not_inject_twice_for_same_turn(self):
        event = self.event()
        first = build_response(event, self.config)
        second = build_response(event, self.config)
        self.assertIn(POLICY_MARKER, first["hookSpecificOutput"]["additionalContext"])
        self.assertEqual(second, {})

    def test_resumed_conversation_new_turn_injects_again(self):
        first = build_response(self.event("turn-before-resume"), self.config)
        resumed = build_response(self.event("turn-after-resume"), self.config)
        self.assertIn(POLICY_MARKER, first["hookSpecificOutput"]["additionalContext"])
        self.assertIn(POLICY_MARKER, resumed["hookSpecificOutput"]["additionalContext"])

    def test_excludes_helper_events(self):
        for event_name in ("TitleGeneration", "Summarization", "Embedding", "Classification"):
            self.assertEqual(build_response(self.event(hook_event_name=event_name), self.config), {})

    def test_injects_truthful_fallback_when_tools_unavailable(self):
        response = build_response(self.event(available_tools=[]), self.config)
        context = response["hookSpecificOutput"]["additionalContext"]
        self.assertIn(FALLBACK_MARKER, context)
        self.assertNotIn(POLICY_MARKER, context)

    def test_disable_and_target_agent_filtering(self):
        disabled = dict(self.config, enabled=False)
        self.assertFalse(is_targeted(self.event(), disabled))
        targeted = dict(self.config, target_agents=["reviewer"])
        coordinator = self.event()
        reviewer = self.event(
            hook_event_name="SubagentStart", agent_type="reviewer", turn_id="review-turn"
        )
        self.assertEqual(build_response(coordinator, targeted), {})
        response = build_response(reviewer, targeted)
        self.assertIn(POLICY_MARKER, response["hookSpecificOutput"]["additionalContext"])

    def test_event_tool_names_accept_codex_mcp_namespace(self):
        namespaced = ["mcp__claude_delegation__" + name for name in REQUIRED_TOOLS]
        self.assertTrue(claude_tools_available(self.event(available_tools=namespaced), self.config))

    def test_runtime_subprocess_trace_preserves_messages_and_tools(self):
        config_path = self.root / "config.json"
        config_path.write_text(json.dumps(self.config), encoding="utf-8")
        event = self.event(turn="trace-turn")
        original_request = {
            "messages": [
                {"role": "system", "content": "Original system"},
                {"role": "user", "content": "Implement the feature"},
            ],
            "tools": [{"name": name} for name in REQUIRED_TOOLS] + [{"name": "other_tool"}],
        }
        environment = dict(os.environ)
        environment["CLAUDE_DELEGATION_HOOK_CONFIG"] = str(config_path)
        result = subprocess.run(
            [
                sys.executable,
                str(Path(__file__).parents[1] / "adapters" / "codex" / "routing_hook.py"),
                str(config_path),
            ],
            input=json.dumps(event),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=5,
            check=True,
            env=environment,
            shell=False,
        )
        hook_output = json.loads(result.stdout)
        outgoing = copy.deepcopy(original_request)
        outgoing["messages"].insert(
            1,
            {
                "role": "developer",
                "content": hook_output["hookSpecificOutput"]["additionalContext"],
            },
        )
        captured = json.loads(json.dumps(outgoing))
        self.assertEqual(captured["messages"][0], original_request["messages"][0])
        self.assertEqual(captured["messages"][-1], original_request["messages"][-1])
        self.assertEqual(captured["tools"], original_request["tools"])
        serialized = json.dumps(captured)
        self.assertEqual(serialized.count(POLICY_MARKER), 1)


if __name__ == "__main__":
    unittest.main()
