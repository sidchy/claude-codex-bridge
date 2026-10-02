---
description: Split a goal into parallel Codex sub-tasks with matched roles, run them, then verify and merge
argument-hint: <goal>
allowed-tools: Bash(python3 ${CLAUDE_PLUGIN_ROOT}/scripts/codex_bridge.py:*), Read, Grep, Glob, Agent
---

Goal: $ARGUMENTS

Run team mode per the `codex-delegate` skill:
1. Recon yourself (or one `explorer` task) so you understand the scope.
2. Decompose into independent sub-tasks with DISJOINT files. For each pick a role: explorer (read-only search) · worker (implement) · debugger (root-cause + fix) · reviewer (critique, read-only) · architect (design, read-only).
3. Dispatch concurrently: write the task list as JSON and run `codex_bridge.py parallel - <<'JSON' ... JSON` (or several `codex-runner` agents / background Bash calls).
4. Verify every result yourself (diff, tests). Optionally run a `reviewer` pass over the combined change.
5. Report: what each role did, what you verified, open issues.
