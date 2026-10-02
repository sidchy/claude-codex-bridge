---
description: Cancel a running background Codex job (default: latest)
argument-hint: [job-id]
allowed-tools: Bash(python3 ${CLAUDE_PLUGIN_ROOT}/scripts/codex_bridge.py:*)
disable-model-invocation: true
---

Run `python3 ${CLAUDE_PLUGIN_ROOT}/scripts/codex_bridge.py cancel $ARGUMENTS` and report; note any partial file changes with `git status`.
