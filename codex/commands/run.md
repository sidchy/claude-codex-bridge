---
description: Codex 的唯一入口：交活、审查、看进度、取消、接管，直接说人话，其余全自动
argument-hint: <说人话：做什么 / 审查 / "它在干嘛" / "停" / "我来接手"；不写则审查当前改动>
allowed-tools: mcp__terminal__run_in_terminal, Bash(${CLAUDE_PLUGIN_ROOT}/scripts/codex_bridge.py:*), Bash(~/.claude/codex-bridge/bin/codex_bridge.py:*), Bash(git:*), Bash(python3 -m unittest:*), Read, Edit, Write, Grep, Glob, Agent, AskUserQuestion
disable-model-invocation: true
---

Handle $ARGUMENTS. Choose role/model/effort/sandbox and execution mode yourself; ask at most one question only if the goal is ambiguous. Invoke the executable directly, with its path quoted. Use this template, replacing the label, role, flags and brief as needed:

```sh
"${CLAUDE_PLUGIN_ROOT}/scripts/codex_bridge.py" run --name task-label --role worker --background - <<'PROMPT'
Goal: describe the task.
Context: paths and known facts.
Do / Don't touch: scope and constraints.
Done when: exact check command.
Reply with: files changed and real check result, at most 10 lines.
PROMPT
```

Every invocation refreshes `~/.claude/codex-bridge/bin/codex_bridge.py`, a stable symlink to the script. Use that executable for subsequent commands below. Quote paths/arguments containing shell metacharacters or spaces; zsh requires correct quoting. Do not prefix the bridge with `python3`.

## Mode and sessions
- Empty request / review / check / second opinion: `review [--base REF|--commit SHA] [focus]`; `--adversarial` for a tough/risky review. No changes to review: say so and stop. Reviews always get fresh sessions.
- Bug/stuck: `run --role debugger`; build/edit/refactor: worker; find/explain: explorer; design: architect. Explorer/reviewer/architect are read-only; worker/debugger follow configured sandbox.
- One need = one `run --name <label>` session. Same label gets or creates that session in this directory; unrelated needs use new labels. `--continue` resumes the newest task session here. Model/effort and sandbox are inherited; concurrent runs on one session are refused.
- Independent needs with disjoint files can use separate `--background` sessions or `parallel tasks.json` (JSON task list, configured `max_parallel`, default 4). Dependent steps run in order: explorer → worker → reviewer. Never let two runs write the same files.
- Bulk work uses ONE sol medium lead which fans out up to 20 luna sub-agents; let the lead split it. Split sessions when needs differ.
- Very simple tasks: do them yourself unless the user explicitly requests Codex (including “强制用 codex”). Simple means most of: ≤2 files / ~15 changed lines, no test-fix loop, a minute's work, quick lookup/explanation, or needs this chat's context. Say “这个很小，我直接做了，没转给 Codex；可说‘强制用 codex’”. Delegate big, slow, parallel work, long reading, test/fix loops, and independent second opinions.

## Choose once, then lock
Assess difficulty before the first call; state your model/effort and reason in one line. Defaults are configurable. Pass overrides only when departing from role defaults or honoring an explicit user choice.

| Task | Model / effort |
|---|---|
| Ordinary build/edit/refactor/recon/explain | gpt-6.1-sol medium |
| Repetitive bulk edits/transforms/extraction/checks (~10+ independent items) | sol medium lead → up to 20 gpt-6-luna sub-agents, high (xhigh for subtle items); or parallel luna tasks |
| Design, tricky logic, ambiguity, sensitive data/migrations/security, hard debugging | gpt-6-astra medium; high if genuinely hard |
| Review | gpt-6-astra high; adversarial same or xhigh if very risky |
| Intricate concurrency/algorithms/major architecture | xhigh rarely; explain why |

Within one session NEVER switch model or effort (prompt cache); all resume paths inherit the original pair. Lead-spawned sub-agents are exempt and follow the injected team policy. On a reasoning/quality verification failure, start a NEW session with a self-contained brief describing the failure: sol medium → astra high → astra xhigh. Do not escalate permission/network failures. Maximum two escalations, then report the outcome and tier.

## Supervision and handoff
- Work taking ~30s+ or open-ended: start `--background`. With `mcp__terminal__run_in_terminal`, open `~/.claude/codex-bridge/bin/codex_bridge.py watch <id>`: one ASCII command line, no cwd. This symlink avoids Chinese/non-ASCII installation paths. For a custom bridge home, copy the printed command including its `CODEX_BRIDGE_HOME=...` prefix; the terminal tool requires an ASCII path.
- No terminal tool: poll `log <id> --since 0`, then use each printed next cursor as `--since`; `status <id> --json` gives compact state. `log <id> --tail N` gives bounded history; `--full` expands details. Relay 1–2 plain lines at checkpoints, not raw logs.
- Progress → watch; replay → log; stop → `cancel <id>` then inspect `git status`; result → `wait <id>` then `result <id>`; takeover → `attach <id>` and give its quoted resume line. Attach refuses active jobs unless `--force`; normally cancel/wait first. Supervision defaults to newest job in this directory, with a printed notice on global fallback; `--name <label>` selects a local label.
- Corrections: identify the exact job and its label, `cancel <id>`, then `run --name <same-label> "<correction>"`. Never use implicit latest. For an unnamed job, use `resume --session <recorded-thread-id>` after cancellation.
- Big/risky work (many files, deletion/renaming, migration, hard to undo, or requested plan): architect first, show plan, ask approve/adjust using AskUserQuestion if available, otherwise normal chat. Existing authorization or “直接做” permits proceeding. Use authorized file/Git/test tools for direct work and verification; if unavailable, report the limitation.
- Codex's done claim needs verification: inspect `git diff` and rerun the required checks yourself. Report briefly in the user's language: actual changes/activity, verified results, open issues, model/tier, and exact `attach <id>` / `log <id>` commands for takeover/replay.

## Workspace memory and quota
- Codex has one global memory for all projects and finds entries by keyword, so look-alike projects used to bleed into each other. The bridge now binds every run to its workspace (`codex_memory=scoped`): it tells Codex which memory entries belong to the run's directory (the directory itself, its Git root or an ancestor inside that root; worktrees also inherit their main checkout, even outside it, while nested independent repos stay separate) and to ignore all others. A brand-new workspace starts with empty memory that grows from runs there. Always pass `--cd <project directory>`; never run project work from a temp directory. `codex_memory=off` disables memory read/write, including `attach`, `on` is Codex's native behaviour.
- When Codex returned a valid quota reading, the activity digest includes `quota: N% of weekly used, resets ...`; `usage` shows the newest available reading by event time. Missing or malformed readings leave quota unavailable. At 80% or more, avoid xhigh and large luna fan-outs unless the user asks, and tell the user the quota is running low.
