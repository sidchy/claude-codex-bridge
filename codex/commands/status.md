---
description: 查看设置和后台任务；也可取回结果或取消（status result / status cancel）
argument-hint: [result|cancel] [任务号，可省略]
allowed-tools: Bash(python3 ${CLAUDE_PLUGIN_ROOT}/scripts/codex_bridge.py:*)
disable-model-invocation: true
---

- No args → run `python3 ${CLAUDE_PLUGIN_ROOT}/scripts/codex_bridge.py status` and `python3 ${CLAUDE_PLUGIN_ROOT}/scripts/codex_bridge.py jobs`; show settings then a compact job table.
- `result [id]` → `python3 ${CLAUDE_PLUGIN_ROOT}/scripts/codex_bridge.py wait [id]` then show the output in full and verify any claimed changes.
- `cancel [id]` → `python3 ${CLAUDE_PLUGIN_ROOT}/scripts/codex_bridge.py cancel [id]`, then check `git status` for partial changes.
(id optional = latest job.) Arguments: $ARGUMENTS
