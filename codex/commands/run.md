---
description: Codex 的唯一入口：交活、审查、看进度、取消、接管，直接说人话，其余全自动
argument-hint: <说人话：做什么 / 审查 / "它在干嘛" / "停" / "我来接手"；不写则审查当前改动>
allowed-tools: mcp__terminal__run_in_terminal, Bash(${CLAUDE_PLUGIN_ROOT}/scripts/codex_bridge.py:*), Read, Grep, Glob, Agent
disable-model-invocation: true
---

User request: $ARGUMENTS

This command is plug-and-play. The user gives plain language only. NEVER ask them to choose a role, model, effort, sandbox, foreground/background, or parallel/serial. Decide everything yourself and just do it; ask a question only if the goal itself is truly ambiguous (one short question max).

0. Supervision requests are part of this command, not separate commands. If the request is about an existing/running Codex run, handle it directly (latest job unless the user names one): "在干嘛/进度/看看" → open a live `watch` in the terminal pane (or `log`); "回看/详细过程" → `log` (`--full` if asked); "停/取消" → `cancel` then report partial changes via `git status`; "我来接手/接管" → `attach` and give the printed `codex resume ...` line; "结果" → `wait` then `result`. Otherwise continue below.
1. Read `${CLAUDE_PLUGIN_ROOT}/docs/routing.md` and choose the mode from the request:
   - empty request → `review` of the current uncommitted changes (if there are none, say so and stop)
   - review / check / second opinion → `review` (add `--adversarial` if the change is risky)
   - stuck / bug → debugger · build / edit / refactor → worker · find / explain → explorer · design first → architect
   - slow or open-ended → `--background` and keep going, then collect the result yourself; independent pieces → run them in parallel
   - "continue / keep going" → `resume`
   Explicit flags in the request (`--model`, `--effort`, `--role`, `--sandbox`) are honored; otherwise use the defaults (`config`).
2. Write a self-contained Codex prompt (it cannot see this chat) and run it via `${CLAUDE_PLUGIN_ROOT}/scripts/codex_bridge.py`, prompt on stdin.
2b. Make it visible: follow "Visibility & control" in the routing guide: long/background runs get a live `watch` in the user's terminal pane, narrate checkpoints, and plan-first (architect → show plan → ask) for big or risky work.
3. Verify the result yourself (diff / tests). 4. Reply briefly in the user's language: what Codex actually did (from the activity digest + a few bullets), what you verified, anything open, and how to take over (`attach`) or see the full transcript (`log`). No tool-flag talk.
