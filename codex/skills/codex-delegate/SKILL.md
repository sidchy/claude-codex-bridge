---
name: codex-delegate
description: Delegate well-specified execution work (bulk edits, refactors, scripts, test-fix loops, long-running implementation) to the local Codex CLI while Claude plans and verifies. Use when the user says "让codex做/用codex/delegate to codex", or when a task is mechanical, large, or parallelizable.
---

# Claude = brain, Codex = hands

Bridge: `python3 ${CLAUDE_PLUGIN_ROOT}/scripts/codex_bridge.py`

## Workflow
1. **Plan first.** Understand the task and read what's needed. Decide exactly what Codex should do.
2. **Write a self-contained prompt** (Codex has no access to this conversation): goal, absolute paths, constraints, acceptance criteria / test command, files that must not change, expected final report format.
3. **Run** (prompt via stdin avoids quoting problems):
   ```bash
   python3 ${CLAUDE_PLUGIN_ROOT}/scripts/codex_bridge.py run --cd <dir> - <<'PROMPT'
   ...
   PROMPT
   ```
   Long jobs: use Bash `run_in_background` and keep working. Independent subtasks can run as parallel background calls (use disjoint files).
4. **Verify**: Codex's final message is a claim, not proof. Check `git diff`, run tests, spot-read output. If wrong, send a follow-up with `resume` (`--session <id>` printed on stderr; default is the most recent session).
5. Report to the user what changed and what you verified.

## Team mode: match the role to the job
| Situation | Role | Defaults |
|---|---|---|
| Find code / gather facts / map a repo | `explorer` | read-only, low |
| Implement a well-specified change, bulk edits, scripts | `worker` | full-access, medium |
| Failing test / bug with unknown cause | `debugger` | full-access, high |
| Second opinion on a diff, audit | `reviewer` | read-only, high |
| Design / trade-off analysis before coding | `architect` | read-only, xhigh |

`codex_bridge.py run --role worker ...` (flags `--model/--effort/--sandbox` still override the role). List with `roles`; add/override roles in settings (`"roles": {"name": {"sandbox":..,"effort":..,"model":..,"preamble":..}}`).

**Parallel**: independent sub-tasks with disjoint files go in one call:
```bash
python3 ${CLAUDE_PLUGIN_ROOT}/scripts/codex_bridge.py parallel - <<'JSON'
[{"name":"scan","role":"explorer","prompt":"..."},
 {"name":"fix-a","role":"worker","cd":"/abs/dir","prompt":"..."},
 {"name":"audit","role":"reviewer","model":"gpt-5.6-terra","prompt":"..."}]
JSON
```
Or spawn `codex-runner` agents (keeps Codex output out of your context). Sequence dependent steps: explorer → worker → reviewer. Codex's own `multi_agent` feature is enabled, so for one big job you may also tell a worker "you may spawn sub-agents for independent parts".

## Hand-off prompt template (keep it tight)
```
Goal: <one sentence>
Context: <paths, relevant facts you already found — don't make Codex rediscover them>
Do: <steps / constraints>   Don't touch: <paths>
Done when: <test/command that must pass>
Reply with: <files changed + check result, <=10 lines>
```

## Settings
Defaults persist in `~/.claude/codex-bridge/settings.json` (initially `gpt-6.1-sol`, effort `medium`, sandbox `danger-full-access`: Codex acts without asking).
- Persistent: `config set model=gpt-5.6-terra effort=high` · `config show` · `config reset` · `models`
- One-off per call: `--role --model --effort --sandbox --cd --add-dir --timeout`
- Effort guide: low = trivial edits; medium = default; high/xhigh = tricky debugging or design-heavy changes. Check `models` for what each model supports.
- Sandbox: `read-only` for analysis/review; `danger-full-access` is the default (user opted in: Codex acts without confirmation); use `workspace-write` or `read-only` per call when the task should be constrained.

## Don't
- Don't delegate trivial one-liners or things needing this conversation's context.
- Don't let two Codex runs write the same files concurrently.
- Don't trust "done" without checking. (Seen in testing: Codex reported pytest crashed inside its sandbox, yet the tests passed when Claude re-ran them.)
