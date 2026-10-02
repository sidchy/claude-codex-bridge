---
description: Codex code review of local changes (add --adversarial to challenge the design; --base/--commit to pick the target)
argument-hint: [--adversarial] [--base REF | --commit SHA] [--background] [focus ...]
allowed-tools: Bash(python3 ${CLAUDE_PLUGIN_ROOT}/scripts/codex_bridge.py:*)
---

Run: `python3 ${CLAUDE_PLUGIN_ROOT}/scripts/codex_bridge.py review $ARGUMENTS` (long reviews: add `--background` via Bash run_in_background or the flag, then `wait`).
Present Codex's findings faithfully, ranked as returned. Then verify the top findings against the code yourself before recommending fixes; say which you confirmed and which you could not.
