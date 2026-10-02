# Codex routing guide (read by /codex:* commands)

Claude = brain, Codex = hands & second pair of eyes. Only applies when the user invoked a /codex:* command.

Bridge script: `../scripts/codex_bridge.py` relative to this docs folder; use its FULL absolute path (the command that sent you here shows it). It is executable: call it directly, e.g. `/abs/path/scripts/codex_bridge.py review --base main`. NEVER prefix `python3`, and NEVER store it in a shell variable (zsh won't word-split it and the call fails with exit 127). Below, `BR` is just shorthand for that absolute path. Defaults: `gpt-6.1-sol`, effort `medium`, **full permission, never asks**. Pick everything yourself; the user shouldn't have to say which mode.

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

## Visibility & control (the user must always see and steer what Codex does)
Codex runs are never a black box: every run is logged (`events.jsonl`) and rendered as a readable timeline (Codex's narration, each command with exit code/output, files changed).
1. **Show the work.** For anything that will take more than ~30s, or any `--background` job: start it in the background, then open a live view for the user. In the desktop app, use the terminal tool (`mcp__terminal__run_in_terminal`, load via ToolSearch if deferred) to run `BR watch <id>` in the user's terminal pane so they watch Codex live. If no terminal tool exists, poll `BR log <id>` and relay new steps in plain language.
2. **Narrate checkpoints** while it runs (what Codex has done so far, what it is on now) in one or two short lines, not raw logs.
3. **Final report always includes** the "Codex activity" digest (commands run, files changed, tokens) plus 2-4 bullets of what Codex actually did, then your own verification result. Offer `BR log <id> --full` for the complete transcript.
4. **Plan first for big or risky work** (many files, deletes/renames, data migrations, anything hard to undo, or the user asked to "see the plan"): run an `architect` pass (read-only), show the plan, and ask the user to approve/adjust (AskUserQuestion) before running `worker`. The user can say "直接做" to skip.
5. **Hand-over / steering.** Tell the user they can take over: `BR attach <id>` prints `codex resume <session>`; run it in a terminal to drive the same Codex session interactively. To correct a running job: `BR cancel <id>` then `BR resume --session <id> "<correction>"`.
