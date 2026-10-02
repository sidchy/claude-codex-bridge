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
Every run is fully recorded and supervised: live timeline in the terminal pane for long jobs, an activity digest (commands, files touched, tokens) in every report, plan-first for big/risky work. `/codex:config` is optional (default gpt-6.1-sol, medium, full permission).
**Model routing**: Claude rates the task first, then picks once: bulk repetitive → sol lead fans out luna sub-agents (high/xhigh); ordinary → gpt-6.1-sol medium; needs intelligence → gpt-6-astra medium/high; review → astra high. Within one Codex session model/effort never change (prompt cache); on failure a NEW session starts one tier up. Defaults editable via `/codex:config`.
**When Codex is worth it**: big multi-file work, long reading, test/fix loops, slow or parallel jobs, independent reviews. Very simple tasks (≤2 files, a few lines, a quick lookup) Claude does itself — delegating would cost more Claude tokens than it saves. Say "强制用 codex" to override.
Nothing triggers automatically; only when you type a /codex command.

Requires `codex` on PATH and `python3`. Settings: `~/.claude/codex-bridge/settings.json`; `CODEX_BRIDGE_HOME` overrides the settings/jobs/cache directory. Every invocation refreshes the executable symlink `~/.claude/codex-bridge/bin/codex_bridge.py` for terminals requiring an ASCII path.

## Roles & parallel
`codex_bridge.py roles` lists presets; `run --role worker ...`; `parallel tasks.json` runs a JSON task list concurrently (each task may set its own role/model/effort/sandbox/cd). Agent `codex:codex-runner` relays one task and returns a short report.

It also covers what the official `codex@openai-codex` plugin does (review, adversarial review, background jobs), so that one was uninstalled.
