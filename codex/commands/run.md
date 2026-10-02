---
description: 把事情交给 Codex —— 直接说你要什么就行，模式/模型/强度全自动
argument-hint: <你要做什么，说人话就行；不写则审查当前改动>
allowed-tools: Bash(python3 ${CLAUDE_PLUGIN_ROOT}/scripts/codex_bridge.py:*), Read, Grep, Glob, Agent
disable-model-invocation: true
---

User request: $ARGUMENTS

This command is plug-and-play. The user gives plain language only. NEVER ask them to choose a role, model, effort, sandbox, foreground/background, or parallel/serial. Decide everything yourself and just do it; ask a question only if the goal itself is truly ambiguous (one short question max).

1. Read `${CLAUDE_PLUGIN_ROOT}/docs/routing.md` and choose the mode from the request:
   - empty request → `review` of the current uncommitted changes (if there are none, say so and stop)
   - review / check / second opinion → `review` (add `--adversarial` if the change is risky)
   - stuck / bug → debugger · build / edit / refactor → worker · find / explain → explorer · design first → architect
   - slow or open-ended → `--background` and keep going, then collect the result yourself; independent pieces → run them in parallel
   - "continue / keep going" → `resume`
   Explicit flags in the request (`--model`, `--effort`, `--role`, `--sandbox`) are honored; otherwise use the defaults (`config`).
2. Write a self-contained Codex prompt (it cannot see this chat) and run it via `python3 ${CLAUDE_PLUGIN_ROOT}/scripts/codex_bridge.py`, prompt on stdin.
3. Verify the result yourself (diff / tests). 4. Reply briefly in the user's language: what Codex did, what you verified, anything open. No tool-flag talk.
