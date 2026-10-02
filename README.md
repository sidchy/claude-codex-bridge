# Codex Bridge (Claude Code plugin)

Claude plans; the local `codex` CLI executes. Default: `gpt-6.1-sol`, effort `medium`, sandbox `workspace-write`.

## Install
```
/plugin marketplace add /absolute/path/to/claude-codex-plugin
/plugin install codex@codex-bridge
```
(or one-off: `claude --plugin-dir /absolute/path/to/claude-codex-plugin/codex`)

## Use
- `/codex:run <task>` — delegate a task
- `/codex:config set model=gpt-5.6-terra effort=high` · `/codex:config models` · `/codex:config reset`
- `/codex:status`
- Or just say "让 codex 去做 …" — the `codex-delegate` skill triggers.

Requires `codex` on PATH and `python3`. Settings: `~/.claude/codex-bridge/settings.json`.
