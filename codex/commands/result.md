---
description: Fetch the output of a finished background Codex job (default: latest)
argument-hint: [job-id]
allowed-tools: Bash(python3 ${CLAUDE_PLUGIN_ROOT}/scripts/codex_bridge.py:*)
---

Run `python3 ${CLAUDE_PLUGIN_ROOT}/scripts/codex_bridge.py result $ARGUMENTS` (if still running, `python3 ${CLAUDE_PLUGIN_ROOT}/scripts/codex_bridge.py wait $ARGUMENTS`). Show the output in full, then verify any claimed changes (diff/tests) before concluding.
