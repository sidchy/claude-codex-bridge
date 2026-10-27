---
description: 可选：改默认模型/强度/权限（一般不用管，默认 gpt-6.1-sol medium 全权限）
argument-hint: [show | set model=.. effort=.. sandbox=.. | reset | models]
allowed-tools: Bash(${CLAUDE_PLUGIN_ROOT}/scripts/codex_bridge.py:*), Bash(python:*), Bash(python.exe:*), Bash(py:*), Bash(powershell.exe:*), Bash(pwsh:*)
disable-model-invocation: true
---

Run `${CLAUDE_PLUGIN_ROOT}/scripts/codex_bridge.py` with the right subcommand for: $ARGUMENTS

## Windows invocation

On native Windows 11 use CPython 3.10+ explicitly with `-X utf8`; no pip packages, administrator rights, WSL, or symlink privileges are required. The Windows launcher is an ordinary Python stub. Do not rely on shebangs, file associations, or a `python3` alias. Substitute the actual interpreter and plugin paths:

```powershell
$env:CODEX_BRIDGE_HOME = 'C:\Users\you\Bridge Data'
& 'C:\Program Files\Python310\python.exe' -X utf8 'C:\plugins\codex\scripts\codex_bridge.py' config show
```

Git Bash with native Windows Python:

```bash
CODEX_BRIDGE_HOME='C:/Users/you/Bridge Data' python -X utf8 "${CLAUDE_PLUGIN_ROOT}/scripts/codex_bridge.py" config show
```

Omit the environment assignment for the default home. Apply the same interpreter prefix to `config set`, `config reset` and `models`. Unix direct execution remains unchanged. Printed Windows supervision/attach commands use PowerShell syntax and the current interpreter path; run them in PowerShell, or translate the prefix/environment assignment for Git Bash. Attach uses `Set-Location -LiteralPath '...'; & ...` for compatibility with Windows PowerShell 5.1.

Printed attach commands may use a Python wrapper to preserve quoted arguments across PowerShell versions. Copy the complete command verbatim.

## Subcommands

- No args / "show" → `config show`
- "set k=v ..." → `config set k=v ...` (keys: model, effort[low|medium|high|xhigh|max|ultra], sandbox[read-only|workspace-write|danger-full-access], timeout, profile, codex_bin, extra_args, roles, model_reasoning, model_bulk, review_model, review_effort, team_policy, max_parallel, subagents[none|auto|active], max_subagents, codex_memory[scoped|off|on])
- "reset" → `config reset`
- "models" → `models` (lists models and the efforts each supports; check effort is supported by the chosen model)
- Natural language ("use terra with high effort") → translate to `config set`.

Print the resulting settings. Default sandbox is danger-full-access (full permission, no confirmations) by the user's choice; read-only roles (explorer/reviewer/architect) stay read-only on purpose.

Routing defaults: `model=gpt-6.1-sol`, `effort=medium`, `model_reasoning=gpt-6-astra`, `model_bulk=gpt-6-luna`, `review_model=gpt-6-astra`, `review_effort=high`, `team_policy=true`, `max_parallel=4`. `config show` prints all of them. Model/effort combinations are checked against the installed CLI catalog before tasks launch. `max_parallel` and timeout must be positive integers; `parallel --max` overrides concurrency for one call.

Structured values must be shell-quoted. These examples use Unix/Git Bash syntax:
- `config set 'roles={"worker":{"preamble":"Keep edits scoped","effort":"high"}}'`: JSON object of role objects. Allowed fields: sandbox (allowed enum above), effort (allowed enum), model (string), preamble (string). Built-in read-only roles remain read-only unless a run explicitly passes `--sandbox`.
- `config set 'extra_args=["--add-dir","/path with spaces"]'`: JSON string array, or `config set 'extra_args=--add-dir "/path with spaces"'` for shell-style argument parsing.

For PowerShell, preserve native JSON quotes with the stop-parsing token (from the repository):

```powershell
python -X utf8 .\codex\scripts\codex_bridge.py --% config set "extra_args=[\"--add-dir\",\"C:/work/project\"]"
```

PowerShell 5.1 strips embedded double quotes from ordinary native arguments, so plain single-quoted JSON is insufficient there. Apply the same escaping to JSON `roles`. String-form arguments use Windows quoting rules on Windows and POSIX shell quoting on Unix; they do not execute a shell. Prefer JSON arrays for lossless paths; JSON requires doubled backslashes, while forward-slash paths avoid that escaping.

When using a Bash tool, execute generated PowerShell lines through `powershell.exe -NoProfile -Command '<complete command>'`, escaping the outer Bash quotes as needed. Copy generated argument-preserving wrappers verbatim.

Invalid structured values are rejected without changing the settings file. Corrupt/unreadable settings stop show/set and execution with the filename. Explicit `config reset` moves the bad file to `settings.json.corrupt-<timestamp>` before restoring defaults. `CODEX_BRIDGE_HOME` overrides the bridge settings/jobs/cache directory; it does not change Codex's own configuration.

`codex_memory`: `scoped` (default) binds Codex's global memory to the run's workspace so look-alike projects cannot bleed into each other; `off` disables memory read and write, including interactive `attach`; `on` is Codex's native behaviour. Scoped memory follows the current Git root and its main checkout for worktrees (inside or outside the main repo); nested independent repos never inherit outer memory. Outside Git, only exact workspace paths match. `usage` prints the newest valid Codex quota reading by event time, when one was returned; quota is otherwise unavailable.

Output defaults: final reports pass through verbatim and in full, with an execution header, mechanical activity and one hint line. `result --full` / `wait --full` print only the saved report. Foreground timelines are off unless `--live` is passed. `log` defaults to a compact execution trace; `log --full` and `watch` show the detailed timeline. Mechanical activity reports step counts/test outcomes, touched files, failed commands/output tails, warning/error events, timeout/interruption, observed sub-agent models/efforts, tokens and quota. `log --failed`, `log --commands` and `log --grep PATTERN` retrieve targeted evidence. Read the full handoff and cross-check it against activity.

Dispatch defaults: `subagents=auto`, `max_subagents=20` (integer 1..20). `run`, `resume` and `steer` accept `--subagents none|auto|active` and `--max-subagents N`; parallel task JSON accepts `subagents` and `max_subagents`. `none` forbids spawning; `active` requests independent parallel sub-agents, model routing and verified integration; both apply to all roles even when `team_policy=false`. `auto` keeps the optional team policy for worker/debugger/architect when enabled. Native review has no prompt and warns that none/active is ignored. Dispatch is a per-task prompt choice and does not change the session model/effort. The activity digest counts distinct observed sub-agent threads (including zero when active).
