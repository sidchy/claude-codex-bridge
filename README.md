# claude-codex-bridge

English | [简体中文](README.zh-CN.md)

A Claude Code plugin that lets Claude hand work to your local [Codex CLI](https://github.com/openai/codex). Claude plans, picks a model, and checks the result. Codex does the typing.

```text
/codex:run fix the failing test in tests/test_login.py
```

Claude reads the request, decides how hard it is, picks a Codex model and effort, starts the run, and tells you what it chose. You can watch Codex work live, replay it later, cancel it, or take over the same Codex session in your own terminal. When Codex says it's done, Claude checks the diff and re-runs the tests before reporting back.

## Install

```text
/plugin marketplace add sidchy/claude-codex-bridge
/plugin install codex@codex-bridge
```

Restart Claude Code and type `/`. You should see `/codex:run` and `/codex:config`.

You need the Codex CLI on your `PATH` (logged in, `npm i -g @openai/codex@latest`) and Python 3.9 or newer. There is nothing to pip install. It has only been tested on macOS. The live watch pane needs the Claude desktop app; in the plain terminal, Claude relays progress instead.

To try it without installing: `git clone` this repo and run `claude --plugin-dir ./claude-codex-bridge/codex`.

If you have OpenAI's official `codex` plugin installed, both register `/codex:*`, so disable one.

## Usage

There is one command. Say what you want.

```text
/codex:run review my current changes
/codex:run review this branch against main, be tough on it
/codex:run rename userId to user_id across the repo
/codex:run what is it doing right now?
/codex:run stop
/codex:run I want to take over
/codex:run
```

An empty request reviews your uncommitted changes. You can still name a model, an effort or a sandbox in the sentence, and Claude will use it.

Nothing runs unless you type a `/codex:` command.

## Models

Claude rates the task before the first call and keeps that choice for the whole Codex session.

| Task | Model |
|---|---|
| Ordinary edits, refactors, exploring, explaining | `gpt-6.1-sol` medium |
| Needs real reasoning: design, tricky logic, migrations, security, hard bugs | `gpt-6-astra` medium, high if really hard |
| Bulk repetitive work (10+ independent items) | a `gpt-6.1-sol` lead that fans out up to 20 `gpt-6-luna` sub-agents |
| Any review | `gpt-6-astra` high |

Inside a session the model and effort never change, because switching would throw away the prompt cache. If a result fails Claude's check for quality reasons, Claude starts a new session one step up (sol medium, astra high, astra xhigh) with a brief explaining what went wrong. That happens at most twice.

Model names depend on your Codex account and CLI version. `/codex:config models` lists yours.

Very small tasks (a couple of files, a few lines, a quick lookup) Claude just does itself, since handing them off costs more tokens than it saves. Say "force Codex" to override.

## Sessions

Each separate need gets its own named Codex session. Follow-ups on the same need reuse it, so Codex keeps its context. Unrelated needs get a new one. Reviews always start fresh so the reviewer isn't grading its own work. Independent jobs on different files can run side by side.

## Watching and taking over

Every run is logged under `~/.claude/codex-bridge/jobs/`.

| Say | What happens |
|---|---|
| "what's it doing?" | Live timeline of Codex's messages, commands, exit codes and sub-agents. In the desktop app it opens in a terminal pane. |
| "show me what happened" | Replays the transcript. |
| "stop" | Kills Codex and everything it started, then lists any partial changes. |
| "tell it to do X instead" | Interrupts the step in progress (its tools are stopped too) and continues the same Codex session with your guidance, so context and prompt cache are kept. It does not start a new session. |
| "I'll take over" | Gives you a `codex resume <session>` line to run yourself. |

Jobs longer than about 30 seconds go to the background on their own. For big or risky changes (many files, deletes, migrations), Claude first gets a read-only plan from Codex and asks you before going ahead. Say "just do it" to skip that.

## Memory and quota

Codex keeps one global memory for all your projects and looks things up by keyword, so similar projects can leak into each other. The bridge binds each run to its workspace: it tells Codex which memory entries belong to the run's directory (the directory itself, or its git project root, so worktrees inherit their project) and to ignore all the others. A new workspace starts with empty memory that builds up from runs there. Set `codex_memory` to `off` to disable memory, or `on` for Codex's native behaviour.

Every report ends with the quota reading Codex itself returns, for example `quota: 48% of weekly used, resets 10-10 09:02`. `usage` shows the latest one. Above 80%, Claude avoids xhigh and large fan-outs.

## Review

Reviews run on `gpt-6-astra` high, read-only. The default target is your uncommitted changes. Ask for a branch (`against main`) or a commit, or say "be tough" for an adversarial pass that goes after the design and ranks findings by severity. Claude then checks the important findings against the code and tells you which ones held up.

## Configuration

`/codex:config` shows and changes these. The settings file is `~/.claude/codex-bridge/settings.json`.

| Key | Default | |
|---|---|---|
| `model` | `gpt-6.1-sol` | lead model for ordinary work |
| `effort` | `medium` | `low`, `medium`, `high`, `xhigh`, `max`, `ultra`, depending on the model |
| `sandbox` | `danger-full-access` | `read-only`, `workspace-write` or `danger-full-access` |
| `model_reasoning` | `gpt-6-astra` | design and hard reasoning |
| `model_bulk` | `gpt-6-luna` | fan-out sub-agents |
| `review_model`, `review_effort` | `gpt-6-astra`, `high` | reviews |
| `team_policy` | `true` | tells the Codex lead when to spawn sub-agents and which model to give them |
| `max_parallel` | `4` | concurrent jobs for `parallel` |
| `codex_memory` | `scoped` | `scoped` binds Codex's global memory to the run's workspace, `off` disables memory, `on` is Codex's native behaviour |
| `timeout` | `1800` | seconds per run |
| `profile` | empty | Codex config profile |
| `codex_bin` | `auto` | newest of the ChatGPT app's bundled CLI and the one on your `PATH` |
| `extra_args` | `[]` | extra arguments for `codex exec` |
| `roles` | `{}` | add or override role presets |

```text
/codex:config set model=gpt-6-astra effort=high
/codex:config set sandbox=workspace-write
/codex:config reset
```

Invalid values are rejected without touching the file. A corrupt file stops everything with an error, and `config reset` keeps a backup before restoring defaults. `CODEX_BRIDGE_HOME` moves the settings, jobs and cache directory.

## Security

Codex runs with `danger-full-access` and approval policy `never` by default. It can read and write anywhere you can and run commands without asking. The explorer, reviewer and architect roles are always read-only. To tighten the rest:

```text
/codex:config set sandbox=workspace-write
```

## The script

The plugin is a thin layer over `codex/scripts/codex_bridge.py`, which you can call directly. It refreshes a stable symlink at `~/.claude/codex-bridge/bin/codex_bridge.py`.

```text
run [--name L] [--role R] [--model M] [--effort E] [--sandbox S] [--cd DIR] [--background] [--continue] -
resume --session ID "<message>"
review [--base REF | --commit SHA] [--adversarial] [--background] [focus ...]
steer [id | --name L] "<message>"
jobs | status | watch | log [--since N] [--tail N] [--full] | attach | wait | result | cancel   # optional job id or --name
parallel tasks.json
roles | models | usage | config [show | set k=v | reset]
```

Roles are `explorer`, `worker`, `debugger`, `reviewer` and `architect`. The prompt behaviour (how Claude rates tasks, when it escalates, when it does the work itself) lives in `codex/commands/run.md`.

## Troubleshooting

A model "is not supported when using Codex with a ChatGPT account": your Codex CLI is too old for it. Run `npm i -g @openai/codex@latest`.

`unknown model`: it isn't in your CLI's catalog. Check `/codex:config models`.

`cannot read settings`: the file is corrupt. Run `/codex:config reset`.

zsh `exit 127` on the script: call it directly with the path quoted. Don't wrap `python3 "..."` in one string or put the path in an unquoted variable.

Nothing shows up under `/`: restart Claude Code and check that `claude plugin list` has `codex@codex-bridge` enabled.

## Development

```bash
python3 -m unittest discover -s codex/tests -v
```

The tests are offline and make no model calls.

## License

[MIT](LICENSE)
