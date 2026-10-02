---
description: Show Codex bridge settings, CLI version and recent/background jobs
allowed-tools: Bash(python3 ${CLAUDE_PLUGIN_ROOT}/scripts/codex_bridge.py:*)
disable-model-invocation: true
---

Run `python3 ${CLAUDE_PLUGIN_ROOT}/scripts/codex_bridge.py status` and `python3 ${CLAUDE_PLUGIN_ROOT}/scripts/codex_bridge.py jobs`, report both compactly (settings first, then a job table).
