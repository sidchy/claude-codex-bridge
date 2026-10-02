---
description: Hand a task to Codex (role, model, effort, --background all optional; Claude picks sensible ones)
argument-hint: [--role R] [--model M] [--effort E] [--sandbox S] [--background] <task>
allowed-tools: Bash(python3 ${CLAUDE_PLUGIN_ROOT}/scripts/codex_bridge.py:*)
disable-model-invocation: true
---

Delegate this to Codex: $ARGUMENTS

First read the routing guide `${CLAUDE_PLUGIN_ROOT}/docs/routing.md`. It tells you how to pick the right mode (review / debugger / worker / explorer / architect / background / parallel / resume) from the request yourself; the user should not have to specify it. You are the planner; Codex is the executor. Follow the routing guide `${CLAUDE_PLUGIN_ROOT}/docs/routing.md` (read it first):
1. Turn the request into a precise, self-contained Codex prompt (goal, files/paths, constraints, acceptance criteria, what NOT to touch).
2. Run it by piping the prompt on stdin:
   `python3 ${CLAUDE_PLUGIN_ROOT}/scripts/codex_bridge.py run [flags] - <<'PROMPT' ... PROMPT`
   Pass through any `--model/--effort/--sandbox` flags the user gave; otherwise use saved defaults.
3. Verify Codex's result yourself (read the diff / run tests) before reporting. Summarize for the user.
