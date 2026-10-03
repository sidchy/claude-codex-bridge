# Codex Bridge for Claude Code

**English** · [简体中文](README.zh-CN.md)

> Claude is the brain. Your local [Codex CLI](https://github.com/openai/codex) is the hands.
> Say what you want in plain language; Claude judges the difficulty, picks the right Codex model, hands the work over, **lets you watch it live**, verifies the result, and reports back.

```
 you ──"/codex:run fix the failing login test"──▶  Claude  (plans · picks model · verifies)
                                                      │ hand-off brief
                                                      ▼
                                            codex_bridge.py  ──▶  codex exec  (your local Codex CLI)
                                                      │               │  └─ optional sub-agents (e.g. up to 20 luna)
                                    full event log ◀──┘               ▼
                          live timeline · replay · take over          files / shell / tests
```

## Why use it

Codex is good at long, mechanical, test-driven work; Claude is good at judging, planning and cross-checking. This plugin lets them cooperate without you babysitting either:

- **One command.** `/codex:run <anything in plain language>`. No flags to remember; Claude chooses role, model, effort, foreground/background and parallel/serial for you.
- **Difficulty-aware model routing.** Ordinary work → `gpt-6.1-sol` medium; work that needs real reasoning → `gpt-6-astra`; repetitive bulk work → a lead that fans out up to 20 `gpt-6-luna` sub-agents; reviews → `gpt-6-astra` high.
- **Cache-friendly sessions.** One need = one named Codex session. Inside a session the model and effort are **never switched** (that would break the prompt cache). If a result fails verification, a *new* session starts one tier up.
- **You can see and steer everything.** Every run is fully recorded. Watch Codex work live, replay the transcript, cancel, or take over the exact same Codex session in your own terminal.
- **Independent review.** `review` (and a tougher `--adversarial` mode) use a fresh astra session so the reviewer never grades its own homework.
- **Saves Claude's tokens on big jobs.** Reading many files, test/fix loops and bulk edits happen inside Codex; Claude only sees a short report. (Tiny tasks are done by Claude itself, because delegating them would cost more than it saves.)
- **Verified, not trusted.** Claude re-checks Codex's "done" claim (git diff, re-running tests) before telling you it worked.

## Requirements

- [Claude Code](https://claude.com/claude-code) (CLI or desktop app) with plugin support.
- The **Codex CLI** on your `PATH`, logged in (`codex --version` works; run `npm i -g @openai/codex@latest`). A recent version is recommended: newer models are rejected by old CLIs.
- `python3` **3.9+** (standard library only, no pip install).
- macOS is what this was built and tested on. Linux should work but is **untested**; Windows is not supported.
- The live "watch" pane needs the **Claude desktop app** (it has a terminal pane). In the plain terminal CLI, Claude relays progress to you instead.

## Install

```text
/plugin marketplace add sidchy/claude-codex-bridge
/plugin install codex@codex-bridge
```

Restart Claude Code (or open a new session). Verify: typing `/` should list `/codex:run` and `/codex:config`.

Update later: `claude plugin marketplace update codex-bridge && claude plugin update codex@codex-bridge`, then restart.
Uninstall: `claude plugin uninstall codex@codex-bridge`.

Try it without installing, from a local clone:

```bash
git clone https://github.com/sidchy/claude-codex-bridge.git
claude --plugin-dir ./claude-codex-bridge/codex
```

> If you also have OpenAI's official `codex` plugin installed, both register `/codex:*` commands. Disable one of them.

## Quick start

```text
/codex:run fix the failing test in tests/test_login.py
/codex:run review my current changes
/codex:run review this branch against main, be tough on it
/codex:run rename the field `userId` to `user_id` across the whole repo
/codex:run what is it doing right now?        # live view
/codex:run stop
/codex:run I want to take over                # prints a `codex resume ...` line
/codex:run                                    # empty request = review uncommitted changes
```

Claude decides the rest. Expect a one-line announcement such as *"Moderately hard, using astra medium"* and, at the end, a short report: what Codex actually did (commands, files touched, tokens), what Claude verified, what is still open, and how to replay or take over.

You can still force things in your sentence: *"use gpt-6-luna with high effort"*, *"read-only"*, *"force Codex"* (see [Small tasks](#small-tasks-are-done-by-claude)).

## Commands

| Command | What it does |
|---|---|
| `/codex:run <plain language>` | The single entry point: hand off a task, review, watch progress, replay, cancel, take over. |
| `/codex:config [show \| set k=v … \| reset \| models]` | Optional. Show or change defaults (model, effort, sandbox, routing). |

Nothing triggers automatically; Codex is only used when you type a `/codex:` command.

## How Claude picks the model

Claude rates the task **before** the first call, announces its choice in one line, and then keeps it for the whole session.

| Task | Model / effort |
|---|---|
| Ordinary build / edit / refactor / explore / explain (~80% of tasks) | `gpt-6.1-sol` · medium |
| Repetitive bulk work (about 10+ independent items: per-file edits, transforms, extraction, mechanical checks) | `gpt-6.1-sol` medium **lead**, which fans out up to **20** `gpt-6-luna` sub-agents at high (xhigh if the items are subtle) |
| Needs real intelligence: design, tricky logic, ambiguous requirements, data-integrity / migration / security-sensitive work, hard root-cause hunts | `gpt-6-astra` · medium (high if genuinely hard) |
| Any review | `gpt-6-astra` · high |
| Intricate concurrency / algorithms / major architecture | xhigh, rarely, with an explanation |

**Lock rule.** Inside one Codex session the model and effort never change; every continue/resume path inherits the original pair (the script ignores overrides and warns). Sub-agents spawned by the Codex lead are exempt.

**Escalation.** If a result fails Claude's verification for *quality/reasoning* reasons, Claude starts a **new** session one tier up (sol medium → astra high → astra xhigh) with a fresh brief describing what failed. Permission or network failures are not escalated. At most two escalations, then Claude reports honestly.

Model names come from your Codex account and CLI version. List what you have with `/codex:config models`, and change the routing defaults with `/codex:config` (see [Configuration](#configuration)).

## Sessions: one need, one session

- `run --name <label>` gets or creates the Codex session called `<label>` in the current directory. Follow-ups on the same need reuse it (Codex keeps its context; you only send the delta). Unrelated needs get new labels.
- `--continue` resumes the newest task session in this directory.
- Reviews and second opinions always start fresh sessions.
- Concurrent runs on the same session are refused, and two runs must never write the same files.
- Independent needs on disjoint files can run side by side in separate background sessions or via `parallel` (default concurrency 4, `max_parallel`).

## Supervision: see it, steer it

Every run is recorded under `~/.claude/codex-bridge/jobs/<id>/` (raw event log, state, result).

| You say / Claude runs | Effect |
|---|---|
| *"what's it doing?"* → `watch <id>` | Live timeline: Codex's narration, each command with exit code and output, sub-agent activity. In the desktop app Claude opens it in a terminal pane for you. |
| *"show me what happened"* → `log <id>` (`--tail N`, `--full`, `--since N`) | Replay. `--since` returns only new events, which keeps Claude's polling cheap. |
| *"stop"* → `cancel <id>` | Terminates Codex **and its whole process tree** (including tools started in their own process group), then reports partial changes. |
| *"I'll take over"* → `attach <id>` | Prints a quoted `codex resume <session>` command. Run it to drive the same Codex session yourself (refused while the job is still running, unless `--force`). |
| `jobs`, `status <id> --json`, `wait <id>`, `result <id>` | Job control; defaults to the newest job in the current directory. |

Every supervision command also accepts `--name <label>` to pick a session by its label instead of an id.

Long jobs (about 30 s or more, or open-ended) run in the background automatically. **Big or risky work** (many files, deletions/renames, migrations, anything hard to undo) starts with a read-only plan from an architect session that Claude shows you for approval; say *"just do it"* to skip.

## Review

`/codex:run review …` runs Codex's native reviewer on `gpt-6-astra` high, **read-only**:

- default target: uncommitted changes; or `--base <ref>`, `--commit <sha>`
- `--adversarial` (say "be tough"): challenges the design, assumes the change can fail in costly ways, ranks findings by severity with file:line and a failure scenario
- Claude then checks the important findings against the code and tells you which it confirmed.

## Small tasks are done by Claude

If a task touches ≤ 2 files / ~15 lines, needs no test-fix loop, is a quick lookup, or depends on this chat's context, Claude just does it and says so in one line. Delegating it would cost more Claude tokens (command text, brief, verification, report) and 15 s+ of Codex start-up than it saves. Say **"force Codex"** (or "强制用 codex") to override.

## Configuration

`/codex:config` (or edit `~/.claude/codex-bridge/settings.json`). `show` prints everything. Defaults:

| Key | Default | Meaning |
|---|---|---|
| `model` | `gpt-6.1-sol` | Lead model for ordinary work |
| `effort` | `medium` | `low` · `medium` · `high` · `xhigh` · `max` · `ultra` (what the chosen model supports) |
| `sandbox` | `danger-full-access` | `read-only` · `workspace-write` · `danger-full-access`; applies to worker/debugger |
| `model_reasoning` | `gpt-6-astra` | For design / hard reasoning (architect role) |
| `model_bulk` | `gpt-6-luna` | For fan-out sub-agents on repetitive work |
| `review_model` / `review_effort` | `gpt-6-astra` / `high` | Reviews |
| `team_policy` | `true` | Tell the Codex lead when and how to spawn sub-agents and with which models |
| `max_parallel` | `4` | Concurrency for `parallel` |
| `timeout` | `1800` | Seconds per run (positive integer) |
| `profile` | `""` | Codex config profile (`-p`) |
| `codex_bin` | `auto` | `auto` = newest of the ChatGPT-app-bundled CLI and your `PATH` one; or `path`, or an explicit binary path |
| `extra_args` | `[]` | Extra arguments for `codex exec` (JSON array or shell-quoted string) |
| `roles` | `{}` | Add/override role presets (`sandbox`, `effort`, `model`, `preamble`) |

Examples:

```text
/codex:config set model=gpt-6-astra effort=high
/codex:config set sandbox=workspace-write
/codex:config set 'roles={"worker":{"preamble":"Keep edits scoped"}}'
/codex:config models
/codex:config reset
```

Bad values are rejected without touching the file. A corrupt settings file stops everything with a clear error; `config reset` backs it up to `settings.json.corrupt-<timestamp>` and restores defaults. Model/effort pairs are validated against your Codex CLI's catalog before a task starts (unknown models are only allowed when a custom profile or provider is configured). `CODEX_BRIDGE_HOME` relocates the bridge's settings/jobs/cache directory.

## Security: read this

By default Codex runs with **`danger-full-access` and approval policy `never`**: it can read and write anywhere your user can and run commands **without asking**. The explorer, reviewer and architect roles are always read-only, but worker and debugger follow your `sandbox` setting. If you do not want that:

```text
/codex:config set sandbox=workspace-write     # or read-only
```

Only point it at work you are comfortable letting an agent execute unattended. The bridge itself makes no network calls and sends nothing anywhere except to your local `codex` binary.

## The bridge script (advanced)

The plugin is a thin layer over one executable, [`codex/scripts/codex_bridge.py`](codex/scripts/codex_bridge.py), which you can also call yourself (it refreshes a stable symlink at `~/.claude/codex-bridge/bin/codex_bridge.py`):

```text
run [--name L] [--role R] [--model M] [--effort E] [--sandbox S] [--cd DIR] [--background] [--continue] [--timeout N] -   # prompt on stdin
resume --session ID "<message>"          review [--base REF | --commit SHA] [--adversarial] [--background] [focus…]
jobs | status [id] [--json] | watch [id] | log [id] [--since N] [--tail N] [--full] | attach [id] | wait [id] | result [id] | cancel [id]
parallel tasks.json                      roles | models | config [show|set k=v|reset]
```

Roles: `explorer` (read-only recon), `worker` (implement), `debugger` (reproduce → root cause → smallest fix), `reviewer` (read-only critique), `architect` (read-only design). Worker/debugger/architect prompts get an injected **team policy** describing when to spawn sub-agents and which model to give them.

## Troubleshooting

| Symptom | Fix |
|---|---|
| `The 'gpt-… ' model is not supported when using Codex with a ChatGPT account` | Your Codex CLI is too old for that model: `npm i -g @openai/codex@latest`. With `codex_bin=auto` the bridge also picks the ChatGPT app's bundled CLI when it is newer. |
| `unknown model '…'; available: …` | The model is not in your CLI's catalog. `/codex:config models`, then `/codex:config set model=…` (or `review_model=` etc.). |
| `cannot read settings …` | Corrupt `settings.json`. `/codex:config reset` (the bad file is kept as `.corrupt-<timestamp>`). |
| zsh `exit 127` / "no such file or directory" for the script | Quote the path and call the script directly. Never `python3 "…"` inside one quoted string, never store it in an unquoted variable. |
| The live-watch terminal rejects the command | The terminal tool accepts only ASCII; use the stable path `~/.claude/codex-bridge/bin/codex_bridge.py`. |
| Nothing appears when you type `/` | Restart Claude Code; check `claude plugin list` shows `codex@codex-bridge` enabled. |

## Limitations (honest ones)

- Difficulty rating, model choice, the "small task" rule and escalation live in the prompt (`codex/commands/run.md`). They are followed by Claude, not enforced by code, so judgement can be off. The parts that *are* enforced in code: the model/effort lock inside a session, sandbox inheritance on resume, cancel/timeout process-tree cleanup, settings validation.
- Which model a Codex sub-agent really used is as reported by Codex; the bridge cannot independently confirm it.
- Tested on macOS with Python 3.9 and 3.14 and Codex CLI 0.160. Model names (`gpt-6.1-sol`, `gpt-6-astra`, `gpt-6-luna`) were available on the author's account and may differ on yours.
- No built-in cost or budget caps; sub-agent fan-out can use a lot of Codex quota.

## Development

```bash
python3 -m unittest discover -s codex/tests -v    # 36 offline tests, no model calls
```

Layout: `.claude-plugin/marketplace.json` (marketplace) · `codex/.claude-plugin/plugin.json` (plugin) · `codex/commands/{run,config}.md` (the prompts Claude follows) · `codex/agents/codex-runner.md` · `codex/scripts/codex_bridge.py` (the bridge) · `codex/tests/`.

Issues and pull requests are welcome.

## License

[MIT](LICENSE)
