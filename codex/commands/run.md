---
description: Codex 的唯一入口：交活、审查、看进度、取消、接管，直接说人话，其余全自动
argument-hint: <说人话：做什么 / 审查 / "它在干嘛" / "停" / "我来接手"；不写则审查当前改动>
allowed-tools: mcp__terminal__run_in_terminal, Bash(${CLAUDE_PLUGIN_ROOT}/scripts/codex_bridge.py:*), Read, Grep, Glob, Agent
disable-model-invocation: true
---

User request: $ARGUMENTS

Plug and play: plain language only. NEVER ask the user to pick role/model/effort/sandbox/foreground/parallel; decide and act (one short question max, only if the goal is truly ambiguous).
Script: `${CLAUDE_PLUGIN_ROOT}/scripts/codex_bridge.py` (call it `BR`). Run it by its FULL path, directly: never prefix `python3`, never put it in a shell variable (zsh exit 127). Prompt on stdin: `BR run [flags] - <<'PROMPT' ... PROMPT`.

## 1. Pick the mode
- Supervision of an existing run (latest unless named): "在干嘛/进度" → live view (see §3); "回看" → `BR log [id]` (`--full`); "停" → `BR cancel` then `git status`; "我来接手" → `BR attach` and give the printed `codex resume …` line; "结果" → `BR wait` then `BR result`.
- empty / review / check / second opinion → `BR review [--base REF|--commit SHA] [focus]`; add `--adversarial` if risky or the user wants it tough. (No changes to review → say so, stop.)
- stuck/bug → `--role debugger` · build/edit/refactor → `--role worker` · find/explain → `--role explorer` · design → `--role architect`.
- Several independent pieces (disjoint files) → `BR parallel` (JSON list); dependent steps run in order (explorer → worker → reviewer).
- Follow-up on the same work ("continue", "also…", fix its findings, same files/goal) → `BR run --continue "<short follow-up>"`: reuses the SAME Codex session of this directory, so Codex keeps its context and you only send the delta. Unrelated task, or reviews/second opinions that need fresh eyes → plain `run` (new session). Never start a new session for a follow-up.
- **Very simple task → do it yourself, don't delegate.** Delegating costs more Claude tokens than it saves (command text, hand-off prompt, verification, report) plus ~15s+ Codex startup. Simple = most of: touches ≤2 files / ≤~15 changed lines, no test-fix loop, answerable in about a minute, a lookup or quick explanation, or depends on this chat's context. Do it directly, then say in one line that it was small enough to do yourself ("这个很小，我直接做了，没转给 Codex") and that they can force Codex by saying "强制用 codex". If the user already said to use Codex (or "强制 codex"), delegate regardless. Delegate when work is big (many files, long reading, test/fix loops), slow, parallelizable, or wants an independent second opinion.

## 2. Model & effort: assess difficulty FIRST, choose once, lock for the session
Before the first call, judge the task (kind + difficulty) and pick ONE model/effort pair. Defaults live in `BR config` (lead gpt-6.1-sol, reasoning gpt-6-astra, bulk gpt-6-luna). Pass `--model/--effort` only when your choice differs from the role's default, or the user named one (the user always wins).
| Task | Model / effort |
|---|---|
| Highly repetitive, simple, many items (bulk edits, per-file/record transforms, extraction, mechanical checks) | lead stays `gpt-6.1-sol` medium and fans out `gpt-6-luna` sub-agents at high (xhigh if items are subtle); or `BR parallel` with luna tasks |
| Ordinary build/edit/refactor/recon/explain (most tasks, ~80%) | `gpt-6.1-sol` medium |
| Needs real intelligence: design, tricky logic, ambiguous/conflicting requirements, data-integrity/migration/security-sensitive, hard root cause | `gpt-6-astra` medium; high if genuinely hard |
| Review (any) | `gpt-6-astra` high (already the default for `review`); `--adversarial` same or xhigh if very risky |
| xhigh | rare: only for intricate work (concurrency, algorithms, major architecture); say why |
Say your pick and reason in ONE line before running (e.g. "难度中等偏上，用 astra medium").
**Lock rule (prompt cache):** inside one Codex session NEVER change model or effort. `--continue`/`resume` always inherit the session's original pair (the script ignores overrides and warns). Sub-agents the Codex lead spawns are exempt; the lead is told (team policy) which model to give them.
**If the result fails your verification:** do NOT bump effort with `--continue`. Start a NEW session one tier up (sol medium → astra high → astra xhigh) with a fresh self-contained brief that says what was tried and why it failed (the old session stays untouched). Max 2 escalations, then report to the user and say which tier was used.

## 3. Supervise (built in, not optional)
- > ~30s or open-ended → `--background`, then open a live view for the user: `mcp__terminal__run_in_terminal` with `<absolute BR path> watch <id>` (ASCII only, one line, no `cwd`). No terminal tool → poll `BR log <id>` and relay.
- Narrate in 1–2 plain lines at checkpoints; no raw logs.
- Big/risky work (many files, delete/rename, migrations, hard to undo, or user wants to see the plan): run `--role architect` first, show the plan, ask approve/adjust (AskUserQuestion), then `worker`. "直接做" skips it.
- Steering a running job: `BR cancel <id>`, then `BR run --continue "<correction>"` (same model/effort, inherited).

## 4. Hand-off prompt (Codex can't see this chat; keep it tight)
`Goal` (1 sentence) · `Context` (paths + facts you already know) · `Do`/`Don't touch` · `Done when` (command that must pass) · `Reply with` (files changed + check result, ≤10 lines).

## 5. Verify and report
Codex's "done" is a claim: check `git diff` and re-run the tests yourself. Reply briefly in the user's language: what Codex actually did (from its activity digest + a few bullets), what you verified, what's open, and how to take over (`attach`) / replay (`log`). Two Codex runs must never write the same files at once.
