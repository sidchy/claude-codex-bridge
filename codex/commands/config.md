---
description: 可选：改默认模型/强度/权限（一般不用管，默认 gpt-6.1-sol medium 全权限）
argument-hint: [show | set model=.. effort=.. sandbox=.. | reset | models]
allowed-tools: Bash(${CLAUDE_PLUGIN_ROOT}/scripts/codex_bridge.py:*)
disable-model-invocation: true
---

Run `${CLAUDE_PLUGIN_ROOT}/scripts/codex_bridge.py` with the right subcommand for: $ARGUMENTS

- No args / "show" → `config show`
- "set k=v ..." → `config set k=v ...` (keys: model, effort[low|medium|high|xhigh|max|ultra], sandbox[read-only|workspace-write|danger-full-access], timeout, profile, extra_args)
- "reset" → `config reset`
- "models" → `models` (lists models and the efforts each supports; check effort is supported by the chosen model)
- Natural language ("use terra with high effort") → translate to `config set`.

Print the resulting settings. Default sandbox is danger-full-access (full permission, no confirmations) by the user's choice; read-only roles (explorer/reviewer/architect) stay read-only on purpose.
