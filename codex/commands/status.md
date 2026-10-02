---
description: 看 Codex 在干什么：实时观看 / 回看过程 / 接管 / 结果 / 取消（status watch|log|attach|result|cancel）
argument-hint: [watch|log|attach|result|cancel] [任务号，可省略]
allowed-tools: mcp__terminal__run_in_terminal, Bash(${CLAUDE_PLUGIN_ROOT}/scripts/codex_bridge.py:*)
disable-model-invocation: true
---

- No args → run `${CLAUDE_PLUGIN_ROOT}/scripts/codex_bridge.py status` and `${CLAUDE_PLUGIN_ROOT}/scripts/codex_bridge.py jobs`; show settings then a compact job table.
- `watch [id]` → open the user's terminal pane (mcp__terminal__run_in_terminal) and run `watch [id]` for a live timeline; if unavailable, show `log [id]`.
- `log [id]` → show the readable transcript (`--full` for untruncated).
- `attach [id]` → run `attach [id]`, and give the user the printed `codex resume ...` line to run in a terminal to take over.
- `result [id]` → `${CLAUDE_PLUGIN_ROOT}/scripts/codex_bridge.py wait [id]` then show the output in full and verify any claimed changes.
- `cancel [id]` → `${CLAUDE_PLUGIN_ROOT}/scripts/codex_bridge.py cancel [id]`, then check `git status` for partial changes.
(id optional = latest job.) Arguments: $ARGUMENTS
