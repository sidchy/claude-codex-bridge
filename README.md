# Codex Bridge (Claude Code plugin)

Claude plans; the local `codex` CLI executes. Default: `gpt-6.1-sol`, effort `medium`, sandbox `danger-full-access` (full permission, never asks).

## Install
```
/plugin marketplace add /absolute/path/to/claude-codex-plugin
/plugin install codex@codex-bridge
```
(or one-off: `claude --plugin-dir /absolute/path/to/claude-codex-plugin/codex`)

## Use (plug and play)
- `/codex:run <说人话描述需求>` — that's it. Claude decides review vs debug vs build vs explore vs design, background vs foreground, parallel vs serial, model/effort; empty request = review current changes.
- `/codex:review [--adversarial]` — shortcut for review. `/codex:status [result|cancel]` — jobs. `/codex:config` — optional defaults (gpt-6.1-sol, medium, full permission).
- Nothing triggers automatically; only when you type a /codex command.

Requires `codex` on PATH and `python3`. Settings: `~/.claude/codex-bridge/settings.json`.

## Roles & parallel
`codex_bridge.py roles` lists presets; `run --role worker ...`; `parallel tasks.json` runs a JSON task list concurrently (each task may set its own role/model/effort/sandbox/cd). Agent `codex:codex-runner` relays one task and returns a short report.

It also covers what the official `codex@openai-codex` plugin does (review, adversarial review, background jobs), so that one was uninstalled.
