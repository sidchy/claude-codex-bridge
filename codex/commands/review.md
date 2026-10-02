---
description: 让 Codex 审查当前改动（加 --adversarial 则专挑设计毛病；其他全自动）
argument-hint: [--adversarial] [关注点，可省略]
allowed-tools: Bash(python3 ${CLAUDE_PLUGIN_ROOT}/scripts/codex_bridge.py:*)
disable-model-invocation: true
---

Run: `python3 ${CLAUDE_PLUGIN_ROOT}/scripts/codex_bridge.py review $ARGUMENTS` (long reviews: add `--background` via Bash run_in_background or the flag, then `wait`).
Present Codex's findings faithfully, ranked as returned. Then verify the top findings against the code yourself before recommending fixes; say which you confirmed and which you could not.
