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
- Trivial one-liner, or needs this chat's context → do it yourself.

## 2. Model / effort / sandbox: do NOT touch
Use the saved defaults (`BR config`: gpt-6.1-sol, medium, full permission). Pass `--model/--effort/--sandbox` ONLY if the user named them in this request. Never raise effort on your own.

## 3. Supervise (built in, not optional)
- > ~30s or open-ended → `--background`, then open a live view for the user: `mcp__terminal__run_in_terminal` with `<absolute BR path> watch <id>` (ASCII only, one line, no `cwd`). No terminal tool → poll `BR log <id>` and relay.
- Narrate in 1–2 plain lines at checkpoints; no raw logs.
- Big/risky work (many files, delete/rename, migrations, hard to undo, or user wants to see the plan): run `--role architect` first, show the plan, ask approve/adjust (AskUserQuestion), then `worker`. "直接做" skips it.
- Steering a running job: `BR cancel <id>`, then `BR run --continue "<correction>"`.

## 4. Hand-off prompt (Codex can't see this chat; keep it tight)
`Goal` (1 sentence) · `Context` (paths + facts you already know) · `Do`/`Don't touch` · `Done when` (command that must pass) · `Reply with` (files changed + check result, ≤10 lines).

## 5. Verify and report
Codex's "done" is a claim: check `git diff` and re-run the tests yourself. Reply briefly in the user's language: what Codex actually did (from its activity digest + a few bullets), what you verified, what's open, and how to take over (`attach`) / replay (`log`). Two Codex runs must never write the same files at once.
