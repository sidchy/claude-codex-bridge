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
**Effort**: default medium (≈80% of tasks); Claude escalates to high/xhigh only when the task is hard or a medium attempt failed verification, continuing the same Codex session, and tells you why.
**When Codex is worth it**: big multi-file work, long reading, test/fix loops, slow or parallel jobs, independent reviews. Very simple tasks (≤2 files, a few lines, a quick lookup) Claude does itself — delegating would cost more Claude tokens than it saves. Say "强制用 codex" to override.
Nothing triggers automatically; only when you type a /codex command.

Requires `codex` on PATH and `python3`. Settings: `~/.claude/codex-bridge/settings.json`.

## Roles & parallel
`codex_bridge.py roles` lists presets; `run --role worker ...`; `parallel tasks.json` runs a JSON task list concurrently (each task may set its own role/model/effort/sandbox/cd). Agent `codex:codex-runner` relays one task and returns a short report.

It also covers what the official `codex@openai-codex` plugin does (review, adversarial review, background jobs), so that one was uninstalled.
