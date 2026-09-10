import unittest

from adapters.codex.e2e_trace import run_trace


class EndToEndLifecycleTests(unittest.TestCase):
    def test_complete_chain_verifies_before_success_and_closes(self):
        _job_id, trace = run_trace()
        self.assertLess(
            trace.index("coordinator independently verifies repository and tests"),
            trace.index("coordinator reports verified outcome"),
        )
        self.assertEqual(trace[-1], "coordinator calls claude_code_close")


if __name__ == "__main__":
    unittest.main()
