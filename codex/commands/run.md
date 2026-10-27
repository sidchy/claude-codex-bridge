---
description: Codex 的唯一入口：交活、审查、看进度、取消、接管，直接说人话，其余全自动
argument-hint: <说人话：做什么 / 审查 / "它在干嘛" / "停" / "我来接手"；不写则审查当前改动>
allowed-tools: mcp__terminal__run_in_terminal, mcp__terminal__stop_terminal_tab, ToolSearch, Bash(${CLAUDE_PLUGIN_ROOT}/scripts/codex_bridge.py:*), Bash(~/.claude/codex-bridge/bin/codex_bridge.py:*), Bash(python:*), Bash(python.exe:*), Bash(py:*), Bash(powershell.exe:*), Bash(pwsh:*), Bash(git:*), Bash(python3 -m unittest:*), Read, Edit, Write, Grep, Glob, Agent, AskUserQuestion
disable-model-invocation: true
---

Handle $ARGUMENTS. Choose role/model/effort/sandbox, sub-agent dispatch and execution mode yourself; ask at most one question only if the goal is ambiguous. On Unix invoke the executable directly, with its path quoted. Use this Unix template, replacing the label, role, flags and brief as needed (Windows instructions follow):

```sh
"${CLAUDE_PLUGIN_ROOT}/scripts/codex_bridge.py" run --name task-label --role worker --subagents auto --background - <<'PROMPT'
Goal: describe the task.
Context: paths and known facts.
Do / Don't touch: scope and constraints.
Done when: exact check command.
Reply with: files changed and real check result, at most 10 lines.
PROMPT
```

Every invocation refreshes `~/.claude/codex-bridge/bin/codex_bridge.py`, a stable symlink on Unix and an unprivileged Python stub on Windows. Use that launcher for subsequent commands below. Quote paths/arguments containing shell metacharacters or spaces; zsh requires correct quoting. On Unix do not prefix the bridge with `python3`.

## Windows invocation

Native Windows 11 with CPython 3.10+ works in PowerShell and Git Bash without WSL, pip packages, administrator rights, or symlink privileges. Always invoke the bridge with Python and `-X utf8`; shebangs and `python3` aliases are not portable. The allowlist permits interpreter launches; use the installed interpreter path when `python` is unavailable.

PowerShell (substitute the actual interpreter, plugin and project paths; a here-string passes the brief verbatim):

```powershell
$env:CODEX_BRIDGE_HOME = 'C:\Users\you\Bridge Data'
$OutputEncoding = [System.Text.UTF8Encoding]::new($false)
@'
Goal: describe the task.
Context: paths and known facts.
Do / Don't touch: scope and constraints.
Done when: exact check command.
Reply with: files changed and real check result, at most 10 lines.
'@ | & 'C:\Program Files\Python310\python.exe' -X utf8 'C:\plugins\codex\scripts\codex_bridge.py' run --name task-label --role worker --subagents auto --background --cd 'C:\src\project' -
```

Git Bash (native Windows Python):

```bash
CODEX_BRIDGE_HOME='C:/Users/you/Bridge Data' python -X utf8 "${CLAUDE_PLUGIN_ROOT}/scripts/codex_bridge.py" run --name task-label --role worker --subagents auto --background --cd 'C:/src/project' - <<'PROMPT'
Goal: describe the task, with the same brief fields as above.
PROMPT
```

Omit the home assignment to use the default. Windows-generated commands use PowerShell syntax with the current interpreter's full path (`& '<python.exe>' -X utf8 '<launcher.py>' watch <id>`). Run those commands in a PowerShell terminal; in Git Bash translate the interpreter invocation and any environment prefix as above. Preserve `$env:CODEX_BRIDGE_HOME = '...';` when printed. Attach uses `Set-Location -LiteralPath '...'; & ...`, including on Windows PowerShell 5.1. Never rewrite a Windows command to direct `.py` execution. Windows process/lock/pipe support uses the standard library and Windows APIs.

Printed attach commands may use a Python wrapper to preserve quoted arguments across PowerShell versions. Copy the complete command verbatim; do not simplify or remove the wrapper.

When using a Bash tool, execute generated PowerShell lines through `powershell.exe -NoProfile -Command '<complete command>'`, escaping the outer Bash quotes as needed.

## Mode and sessions
- Empty request / review / check / second opinion: `review [--base REF|--commit SHA] [focus]`; `--adversarial` for a tough/risky review. No changes to review: say so and stop. Reviews always get fresh sessions.
- Bug/stuck: `run --role debugger`; build/edit/refactor: worker; find/explain: explorer; design: architect. Explorer/reviewer/architect are read-only; worker/debugger follow configured sandbox.
- One need = one `run --name <label>` session. Same label gets or creates that session in this directory; unrelated needs use new labels. `--continue` resumes the newest task session here. Model/effort and sandbox are inherited; concurrent runs on one session are refused.
- Independent needs with disjoint files can use separate `--background` sessions or `parallel tasks.json` (JSON task list, configured `max_parallel`, default 4). Dependent steps run in order: explorer → worker → reviewer. Never let two runs write the same files.
- Bulk work uses `--subagents active`: ONE sol medium lead fans out up to 20 luna sub-agents; let the lead split it. Split sessions when needs differ.
- Very simple tasks: do them yourself unless the user explicitly requests Codex (including “强制用 codex”). Simple means most of: ≤2 files / ~15 changed lines, no test-fix loop, a minute's work, quick lookup/explanation, or needs this chat's context. Say “这个很小，我直接做了，没转给 Codex；可说‘强制用 codex’”. Delegate big, slow, parallel work, long reading, test/fix loops, and independent second opinions.

- Choose and pass `--subagents` explicitly for every task: `--subagents active` for large work that splits independently (roughly 10+ similar items, several independent modules/directories, broad multi-area recon or review); `--subagents none` for small, serial, tightly coupled, order-dependent work, shared-file edits, or one coherent design; otherwise `--subagents auto`. `--max-subagents N` limits concurrency to 1..20 (default 20). These are prompt controls, not model/effort locks; they may change on resume or steer. For review needing per-call dispatch, use `run --role reviewer --subagents active` with an explicit review brief; native `review` cannot carry the control and warns when configured none/active is ignored.

## Choose once, then lock
Assess difficulty before the first call; state your model/effort, sub-agent mode and reason in one line. Defaults are configurable. Pass overrides only when departing from role defaults or honoring an explicit user choice.

| Task | Model / effort |
|---|---|
| Ordinary build/edit/refactor/recon/explain | gpt-6.1-sol medium |
| Repetitive bulk edits/transforms/extraction/checks (~10+ independent items) | `--subagents active`: sol medium lead → up to 20 gpt-6-luna sub-agents, high (xhigh for subtle items); or parallel luna tasks |
| Design, tricky logic, ambiguity, sensitive data/migrations/security, hard debugging | gpt-6-astra medium; high if genuinely hard |
| Review | gpt-6-astra high; adversarial same or xhigh if very risky |
| Intricate concurrency/algorithms/major architecture | xhigh rarely; explain why |

Within one session NEVER switch model or effort (prompt cache); all resume paths inherit the original pair. Lead-spawned sub-agents are exempt and follow the injected team policy. On a reasoning/quality verification failure, start a NEW session with a self-contained brief describing the failure: sol medium → astra high → astra xhigh. Do not escalate permission/network failures. Maximum two escalations, then report the outcome and tier.

## Supervision and handoff
- Work taking ~30s+ or open-ended: start `--background`, and in the SAME turn, right after the job id is printed, open the live view for the user. This is a required step, not an option; the user watches Codex work in the desktop app's terminal pane, and skipping it leaves them blind.
  1. If `mcp__terminal__run_in_terminal` is not callable, it is a deferred tool: load it first with ToolSearch, query `select:mcp__terminal__run_in_terminal`. Never conclude "no terminal tool" without trying this.
  2. Call it with `command` = the printed `watch` line (on Unix normally `~/.claude/codex-bridge/bin/codex_bridge.py watch <id>`; on Windows the explicit PowerShell interpreter command above; no `cwd`, `title` = a short label such as "Codex: <task>"). Use a terminal matching the printed shell syntax. Open one tab per background job, every time, including when an earlier tab exists. With a custom bridge home, preserve the printed home assignment (`CODEX_BRIDGE_HOME=...` on Unix, `$env:CODEX_BRIDGE_HOME = '...';` on Windows).
  3. When the job finishes, close that tab (`mcp__terminal__stop_terminal_tab` with `close: true`) so the pane does not pile up.
  4. Only if the tool truly does not exist or is denied: say so and give the user the exact `watch` command to run in their own terminal; then relay progress yourself with `status <id> --json`.
- No terminal tool: poll `status <id> --json` for compact state, activity counts and exit code. Relay 1–2 plain lines at checkpoints, not raw logs.
- Progress → watch; requested replay → log; stop → `cancel <id>` then inspect `git status`; result → `wait <id>` (full report and activity); takeover → `attach <id>` and give its quoted resume line. Attach refuses active jobs unless `--force`; normally cancel/wait first. Supervision defaults to newest job in this directory, with a printed notice on global fallback; `--name <label>` selects a local label.
- Corrections / guidance while Codex is working (including after watching the live pane): `steer <id> "<message>"` or `steer --name <label> "<message>"`. This interrupts the step in progress, stops its tools, and resumes the SAME session with the guidance; it is not mid-turn message injection. NEVER cancel and start a new session just to correct or redirect. Resumed sessions keep their full configured sandbox, network access and .git write capability since 0.8.0, so continuing the same session is correct for commit/network work too. New sessions are only for unrelated needs, fresh-eyes review, or escalation after failed verification. Steer defaults to background (open its new watch pane); `--foreground` runs inline, and `-` reads guidance from stdin.
- Big/risky work (many files, deletion/renaming, migration, hard to undo, or requested plan): architect first, show plan, ask approve/adjust using AskUserQuestion if available, otherwise normal chat. Existing authorization or “直接做” permits proceeding. Use authorized file/Git/test tools for direct work and verification; if unavailable, report the limitation.
- Reporting: Codex's report comes through in full; read it as the handoff and cross-check it against the mechanical activity block, especially failed commands or touched files absent from the prose. `log <id>` is a compact execution trace; `log <id> --full` is the detailed transcript, and `watch` stays detailed for humans. Retrieve targeted details whenever anything looks missing or ambiguous: `log <id> --failed` shows failed commands and their last 3 output lines; `log <id> --commands` lists each command and exit code; `log <id> --grep PATTERN` searches detailed rendered lines with 2 lines of context. `--since N` and `--tail N` also select the detailed render. Use targeted retrieval, not blanket transcript reads; never declare success when activity shows failures without investigating them. `result <id> --full` prints only the saved report. If activity says the agent count may be incomplete, dispatch is not mechanically verified; do not infer a count from Codex's prose. Always pass Codex's disclosed problems/deviations/open questions on to the user. Relay the handoff and your own verification in your own words; never paste Codex text. `--live` is for people who explicitly want a foreground timeline; omit it for delegation.
- Codex's done claim needs verification: inspect `git diff` and rerun the required checks yourself. Report briefly in the user's language: actual changes/activity, verified results, open issues, model/tier, and exact `attach <id>` / `log <id>` commands for takeover/replay.

## Workspace memory and quota
- Codex has one global memory for all projects and finds entries by keyword, so look-alike projects used to bleed into each other. The bridge now binds every run to its workspace (`codex_memory=scoped`): it tells Codex which memory entries belong to the run's directory (the directory itself, its Git root or an ancestor inside that root; worktrees also inherit their main checkout, even outside it, while nested independent repos stay separate) and to ignore all others. A brand-new workspace starts with empty memory that grows from runs there. Always pass `--cd <project directory>`; never run project work from a temp directory. `codex_memory=off` disables memory read/write, including `attach`, `on` is Codex's native behaviour.
- When Codex returned a valid quota reading, the activity digest includes `quota: N% of weekly used, resets ...`; `usage` shows the newest available reading by event time. Missing or malformed readings leave quota unavailable. At 80% or more, avoid xhigh and large luna fan-outs unless the user asks, and tell the user the quota is running low.
