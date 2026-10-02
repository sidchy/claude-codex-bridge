---
description: Show or change Codex bridge settings (model, reasoning effort, sandbox, timeout)
argument-hint: [show | set model=.. effort=.. sandbox=.. | reset | models]
allowed-tools: Bash(python3 ${CLAUDE_PLUGIN_ROOT}/scripts/codex_bridge.py:*)
---

Run `python3 ${CLAUDE_PLUGIN_ROOT}/scripts/codex_bridge.py` with the right subcommand for: $ARGUMENTS

- No args / "show" → `config show`
- "set k=v ..." → `config set k=v ...` (keys: model, effort[low|medium|high|xhigh|max|ultra], sandbox[read-only|workspace-write|danger-full-access], timeout, profile, extra_args)
- "reset" → `config reset`
- "models" → `models` (lists models and the efforts each supports; check effort is supported by the chosen model)
- Natural language ("use terra with high effort") → translate to `config set`.

Print the resulting settings. Default sandbox is danger-full-access (full permission, no confirmations) by the user's choice; read-only roles (explorer/reviewer/architect) stay read-only on purpose.
