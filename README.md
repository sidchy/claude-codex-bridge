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
- `/codex:team <goal>` — split into parallel Codex sub-tasks with matched roles (explorer/worker/debugger/reviewer/architect)
- `/codex:status`
- Or just say "让 codex 去做 …" — the `codex-delegate` skill triggers.

Requires `codex` on PATH and `python3`. Settings: `~/.claude/codex-bridge/settings.json`.

## Roles & parallel
`codex_bridge.py roles` lists presets; `run --role worker ...`; `parallel tasks.json` runs a JSON task list concurrently (each task may set its own role/model/effort/sandbox/cd). Agent `codex:codex-runner` relays one task and returns a short report.
