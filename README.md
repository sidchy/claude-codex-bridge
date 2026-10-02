# Codex Bridge (Claude Code plugin)

Claude plans; the local `codex` CLI executes. Default: `gpt-6.1-sol`, effort `medium`, sandbox `danger-full-access` (full permission, never asks).

## Install
```
/plugin marketplace add /absolute/path/to/claude-codex-plugin
/plugin install codex@codex-bridge
```
(or one-off: `claude --plugin-dir /absolute/path/to/claude-codex-plugin/codex`)

## Use
Only one thing to remember: **`/codex:run <说人话>`**. It does everything: hand off a task, review (add "要狠一点" for adversarial), watch progress ("它在干嘛"), cancel ("停"), replay ("回看过程"), take over ("我来接手"). Empty request = review current changes.
Every run is fully recorded and supervised: live timeline in the terminal pane for long jobs, an activity digest (commands, files changed, tokens) in every report, plan-first for big/risky work. `/codex:config` is optional (default gpt-6.1-sol, medium, full permission).
Nothing triggers automatically; only when you type a /codex command.

Requires `codex` on PATH and `python3`. Settings: `~/.claude/codex-bridge/settings.json`.

## Roles & parallel
`codex_bridge.py roles` lists presets; `run --role worker ...`; `parallel tasks.json` runs a JSON task list concurrently (each task may set its own role/model/effort/sandbox/cd). Agent `codex:codex-runner` relays one task and returns a short report.

It also covers what the official `codex@openai-codex` plugin does (review, adversarial review, background jobs), so that one was uninstalled.
