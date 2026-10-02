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

## Settings
Defaults persist in `~/.claude/codex-bridge/settings.json` (initially `gpt-6.1-sol`, effort `medium`, sandbox `workspace-write`).
- Persistent: `config set model=gpt-5.6-terra effort=high` · `config show` · `config reset` · `models`
- One-off per call: `--model --effort --sandbox --cd --add-dir --timeout`
- Effort guide: low = trivial edits; medium = default; high/xhigh = tricky debugging or design-heavy changes. Check `models` for what each model supports.
- Sandbox: `read-only` for analysis/review; `workspace-write` for edits; `danger-full-access` only on explicit user request.

## Don't
- Don't delegate trivial one-liners or things needing this conversation's context.
- Don't let two Codex runs write the same files concurrently.
- Don't trust "done" without checking.
