---
description: Hand a task to Codex (role, model, effort, --background all optional; Claude picks sensible ones)
argument-hint: [--role R] [--model M] [--effort E] [--sandbox S] [--background] <task>
allowed-tools: Bash(python3 ${CLAUDE_PLUGIN_ROOT}/scripts/codex_bridge.py:*)
---

Delegate this to Codex: $ARGUMENTS

You are the planner; Codex is the executor. Follow the `codex-delegate` skill:
1. Turn the request into a precise, self-contained Codex prompt (goal, files/paths, constraints, acceptance criteria, what NOT to touch).
2. Run it by piping the prompt on stdin:
   `python3 ${CLAUDE_PLUGIN_ROOT}/scripts/codex_bridge.py run [flags] - <<'PROMPT' ... PROMPT`
   Pass through any `--model/--effort/--sandbox` flags the user gave; otherwise use saved defaults.
3. Verify Codex's result yourself (read the diff / run tests) before reporting. Summarize for the user.
