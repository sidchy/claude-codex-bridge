---
description: Delegate a task to the local Codex CLI (Claude plans, Codex executes)
argument-hint: [--model M] [--effort E] [--sandbox S] <task>
allowed-tools: Bash(python3 ${CLAUDE_PLUGIN_ROOT}/scripts/codex_bridge.py:*)
---

Delegate this to Codex: $ARGUMENTS

You are the planner; Codex is the executor. Follow the `codex-delegate` skill:
1. Turn the request into a precise, self-contained Codex prompt (goal, files/paths, constraints, acceptance criteria, what NOT to touch).
2. Run it by piping the prompt on stdin:
   `python3 ${CLAUDE_PLUGIN_ROOT}/scripts/codex_bridge.py run [flags] - <<'PROMPT' ... PROMPT`
   Pass through any `--model/--effort/--sandbox` flags the user gave; otherwise use saved defaults.
3. Verify Codex's result yourself (read the diff / run tests) before reporting. Summarize for the user.
