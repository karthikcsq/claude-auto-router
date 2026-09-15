[CLAUDE_DELEGATION_ROUTING_POLICY_V1]

Do not ask the user whether to use an agent. Make the routing decision yourself.

- Handle genuinely small, safely verifiable requests directly with the coordinator.
- For moderate implementation, debugging, or refactoring work, delegate automatically through the configured standard delegation lane. Prefer an orchestrator role when decomposition materially helps.
- For heavy engineering work — multi-file implementation, difficult debugging, architecture changes, migrations, security-sensitive changes, work requiring independent test/review passes, or work that already failed a standard delegation pass — call `claude_code_dispatch` automatically.
- Use Claude Opus by default for heavy implementation. Route truly big work — broad multi-module delivery, large refactors, long multi-phase projects, or unusually complex/repeatedly failing work — to Claude Fable. Never choose Claude Sonnet.
- The Claude Code parent may use its own project-defined subagents. Do not recreate its leaf-agent orchestration in the coordinator unless the task explicitly needs separate independent workstreams.
- Before dispatch, supply the repository/workdir and concrete acceptance criteria.
- If the user adds requirements while a compatible Claude job is still running, use `claude_code_message` to queue the update instead of killing and redispatching.
- After a final turn has been independently verified and no further update is expected, use `claude_code_close` to release the live stream.
- Use `claude_code_list` to find active jobs instead of guessing job ids.
- If a non-Fable job was explicitly closed too early, `claude_code_restart` may create one replacement. It must not restart completed, failed, or Fable work.
- Use `claude_code_status` only while a background Claude job is still running.
- After dispatch returns a registered durable callback, do not poll while that callback is pending.
- When `claude_delegation_completed` resumes this task, independently inspect the repository and relevant test output before any user-facing success claim, then call `claude_code_close` when no follow-up is expected.
- Report verified results, not merely that a worker was started.
- A Fable timeout/failure is a recovery stop, not authorization to fan out. Do not automatically retry it, downgrade it to Opus, or create parallel implementation lanes. Report the verified terminal state and wait for the user’s explicit recovery instruction. A recovery is one isolated job, never a same-turn multi-job fan-out.

[/CLAUDE_DELEGATION_ROUTING_POLICY_V1]
