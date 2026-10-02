---
name: codex-delegate
description: The single entry point for using the local Codex CLI from Claude. Use it automatically (the user should never have to choose) when the user mentions codex / 让codex做, wants a code review or second opinion, when Claude is stuck on a bug, when work is bulk/mechanical/long-running/parallelizable, or before declaring a significant change finished. Claude plans and verifies; Codex executes, reviews, or investigates.
---

# Claude = brain, Codex = hands & second pair of eyes

Bridge: `python3 ${CLAUDE_PLUGIN_ROOT}/scripts/codex_bridge.py` (call it `BR` below). Defaults: `gpt-6.1-sol`, effort `medium`, **full permission, never asks**. Pick everything yourself; the user shouldn't have to say which mode.

## Route the request (decide silently)
| Situation | Do |
|---|---|
| "review my changes / check this" | `BR review` (native reviewer; target auto: uncommitted, or `--base main`, `--commit SHA`) |
| High-stakes, design-heavy, security/data/migration change, or "be tough on it" | `BR review --adversarial "<focus>"` |
| Finished a significant change yourself | Offer/perform a `review` pass before saying done |
| Stuck / failing test / unknown root cause / want a 2nd opinion | `BR run --role debugger` (fresh eyes; give it the symptom + what you already tried) |
| Clear implementation, bulk edits, scripts, refactors | `BR run --role worker` |
| "Where is X / how does Y work" recon | `BR run --role explorer` |
| Design/trade-off analysis before coding | `BR run --role architect` |
| Task will take > ~2 min or is open-ended | add `--background`; keep working; later `BR jobs`, `BR wait [id]`, `BR result [id]`, `BR cancel [id]` |
| Several independent pieces (disjoint files) | `BR parallel` (JSON list; per-task role/model/effort) or `/codex:team`; sequence dependent steps explorer → worker → reviewer |
| Follow-up on earlier Codex work ("continue", "dig deeper", fix its findings) | `BR resume [--session ID] "<msg>"` (default: most recent) |
| Trivial one-liner or needs this conversation's context | Do it yourself; don't delegate |

Model/effort: leave defaults unless the job warrants more: `--effort high|xhigh` for tricky debugging/design/adversarial review, `--effort low` for recon. Switch model with `--model gpt-5.6-terra` etc. (`BR models` lists them); persist with `BR config set model=.. effort=..`. Sandbox: default full access; use `--sandbox read-only` if a task must not change files (explorer/reviewer/architect already are).

## Hand-off prompt (Codex can't see this chat; keep it tight)
```
Goal: <one sentence>
Context: <paths + facts you already found — don't make Codex rediscover>
Do: <steps/constraints>   Don't touch: <paths>
Done when: <command that must pass>
Reply with: <files changed + check result, <=10 lines>
```
Run with the prompt on stdin: `BR run --role worker --cd <dir> - <<'PROMPT' ... PROMPT`. For output you don't want in your context, use the `codex-runner` agent (it relays one task and returns a short report; one per parallel task).

## After Codex returns
Its message is a claim, not proof: check `git diff`, re-run the tests yourself (seen in testing: Codex said pytest crashed; it passed when Claude re-ran it). If wrong, `resume` with specifics. Report to the user: what changed, what you verified, what's open.

## Rules
- Two Codex runs must never write the same files concurrently.
- Codex's own multi-agent is on: for one big job you may tell a worker "you may spawn sub-agents for independent parts".
- Review findings: confirm the important ones against the code before acting; label unconfirmed ones.
