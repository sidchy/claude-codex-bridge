# Codex Bridge (Claude Code plugin)

Claude plans; the local `codex` CLI executes. Default: `gpt-6.1-sol`, effort `medium`, sandbox `danger-full-access` (full permission, never asks).

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
- `/codex:review [--adversarial] [--base REF]` · `/codex:status` (settings + jobs) · `/codex:result` · `/codex:cancel`
- `--background` on run/review returns a job id; check with status/result/cancel
- Nothing triggers automatically: Claude only uses Codex when you type a /codex command. `/codex:run <anything>` picks the mode for you (see docs/routing.md).

Requires `codex` on PATH and `python3`. Settings: `~/.claude/codex-bridge/settings.json`.

## Roles & parallel
`codex_bridge.py roles` lists presets; `run --role worker ...`; `parallel tasks.json` runs a JSON task list concurrently (each task may set its own role/model/effort/sandbox/cd). Agent `codex:codex-runner` relays one task and returns a short report.

It also covers what the official `codex@openai-codex` plugin does (review, adversarial review, background jobs), so that one was uninstalled.
