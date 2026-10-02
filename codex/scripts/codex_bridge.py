#!/usr/bin/env python3
"""Bridge between Claude Code and the local `codex` CLI.

Subcommands:
  run     [flags] [PROMPT|-]   run a task via `codex exec` (prompt from arg or stdin)
  resume  [flags] [PROMPT|-]   continue the last (or --session ID) Codex session
  config  [show|set k=v ...|reset]   persistent defaults
  models                       list models/efforts from Codex's local cache
  status                       codex version + effective defaults

Persistent settings: ~/.claude/codex-bridge/settings.json
Per-call flags override them: --model --effort --sandbox --cd --add-dir --timeout --profile
"""
import argparse, json, os, shutil, subprocess, sys, tempfile

SETTINGS = os.path.expanduser("~/.claude/codex-bridge/settings.json")
DEFAULTS = {
    "model": "gpt-6.1-sol",
    "effort": "medium",
    "sandbox": "workspace-write",  # read-only | workspace-write | danger-full-access
    "timeout": 1800,
    "profile": "",
    "codex_bin": "auto",  # auto = newest of ChatGPT.app bundled CLI and PATH; or "path", or a binary path
    "extra_args": [],
}
EFFORTS = {"low", "medium", "high", "xhigh", "max", "ultra"}
SANDBOXES = {"read-only", "workspace-write", "danger-full-access"}


def load():
    s = dict(DEFAULTS)
    try:
        with open(SETTINGS) as f:
            s.update(json.load(f))
    except (OSError, ValueError):
        pass
    return s


def save(s):
    os.makedirs(os.path.dirname(SETTINGS), exist_ok=True)
    with open(SETTINGS, "w") as f:
        json.dump(s, f, indent=2, ensure_ascii=False)


def validate(k, v):
    if k == "effort" and v not in EFFORTS:
        sys.exit(f"effort must be one of {sorted(EFFORTS)}")
    if k == "sandbox" and v not in SANDBOXES:
        sys.exit(f"sandbox must be one of {sorted(SANDBOXES)}")
    if k == "timeout":
        return int(v)
    if k == "extra_args":
        return v.split() if isinstance(v, str) else v
    if k not in DEFAULTS:
        sys.exit(f"unknown key '{k}'. keys: {', '.join(DEFAULTS)}")
    return v


GUI_BIN = "/Applications/ChatGPT.app/Contents/Resources/codex-cli/CodexCLI.app/Contents/MacOS/codex"


def _ver(b):
    try:
        out = subprocess.run([b, "--version"], capture_output=True, text=True, timeout=10).stdout
        return tuple(int(x) for x in out.split()[-1].split("."))
    except Exception:
        return (0,)


def codex_bin():
    cfg = load()["codex_bin"]
    if cfg == "auto":  # newest of ChatGPT.app's bundled CLI and PATH
        cands = [b for b in (shutil.which("codex"), GUI_BIN) if b and os.access(b, os.X_OK)]
        if cands:
            return max(cands, key=_ver)
    elif cfg != "path" and os.access(cfg, os.X_OK):
        return cfg
    p = shutil.which("codex")
    if not p:
        sys.exit("codex CLI not found in PATH (npm i -g @openai/codex)")
    return p


def cmd_config(a):
    s = load()
    if a.action == "reset":
        save(dict(DEFAULTS)); print("reset to defaults"); s = dict(DEFAULTS)
    elif a.action == "set":
        for kv in a.pairs:
            if "=" not in kv:
                sys.exit(f"expected key=value, got {kv}")
            k, v = kv.split("=", 1)
            s[k] = validate(k, v)
        save(s)
    print(json.dumps(s, indent=2, ensure_ascii=False))


def cmd_models(_):
    p = os.path.expanduser("~/.codex/models_cache.json")
    try:
        models = json.load(open(p))["models"]
    except Exception as e:
        sys.exit(f"cannot read {p}: {e} (run `codex` once to populate)")
    for m in models:
        if m.get("visibility") != "list":
            continue
        lv = [r.get("effort") for r in m.get("supported_reasoning_levels", [])]
        print(f"{m['slug']:<18} default={m.get('default_reasoning_level')}  efforts={','.join(lv)}")


def cmd_status(_):
    print(subprocess.run([codex_bin(), "--version"], capture_output=True, text=True).stdout.strip())
    print("settings file:", SETTINGS)
    print(json.dumps(load(), indent=2, ensure_ascii=False))


def run_codex(a, resume):
    s = load()
    model = a.model or s["model"]
    effort = a.effort or s["effort"]
    sandbox = a.sandbox or s["sandbox"]
    timeout = a.timeout or s["timeout"]
    profile = a.profile or s["profile"]
    validate("effort", effort); validate("sandbox", sandbox)

    prompt = a.prompt
    if prompt in (None, "-"):
        prompt = sys.stdin.read()
    if not prompt.strip():
        sys.exit("empty prompt")

    out = tempfile.NamedTemporaryFile(suffix=".txt", delete=False).name
    cmd = [codex_bin(), "exec"]
    if resume:
        cmd.append("resume")
        cmd += [a.session] if a.session else ["--last"]
    cmd += ["--json", "--skip-git-repo-check", "-o", out,
            "-m", model, "-c", f'model_reasoning_effort="{effort}"']
    if profile:
        cmd += ["-p", profile]
    if not resume:  # `resume` doesn't accept -s/-C/--add-dir; it inherits the session
        cmd += ["-s", sandbox, "-C", os.path.abspath(a.cd or os.getcwd())]
        for d in a.add_dir or []:
            cmd += ["--add-dir", d]
    cmd += s["extra_args"] + ["-"]

    print(f"[codex] model={model} effort={effort} sandbox={sandbox if not resume else '(session)'}",
          file=sys.stderr)
    try:
        p = subprocess.run(cmd, input=prompt, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        sys.exit(f"codex timed out after {timeout}s")

    thread, msgs, errors, usage = None, [], [], None
    for line in p.stdout.splitlines():
        try:
            ev = json.loads(line)
        except ValueError:
            continue
        t = ev.get("type")
        if t == "thread.started":
            thread = ev.get("thread_id")
        elif t == "item.completed":
            it = ev.get("item", {})
            if it.get("type") == "agent_message":
                msgs.append(it.get("text", ""))
            elif it.get("type") == "error":
                errors.append(it.get("message", ""))
        elif t in ("error", "turn.failed"):
            errors.append(json.dumps(ev.get("error") or ev.get("message"), ensure_ascii=False))
        elif t == "turn.completed":
            usage = ev.get("usage")

    try:
        final = open(out).read().strip()
        os.unlink(out)
    except OSError:
        final = ""
    final = final or (msgs[-1] if msgs else "")

    if p.returncode != 0 or (not final and errors):
        print(f"CODEX FAILED (exit {p.returncode})", file=sys.stderr)
        for e in errors:
            print(e, file=sys.stderr)
        if not errors:
            print(p.stderr[-2000:], file=sys.stderr)
        sys.exit(p.returncode or 1)

    print(final)
    print(f"\n---\n[codex session={thread} tokens={usage}] resume with: "
          f"codex_bridge.py resume --session {thread} \"<follow-up>\"", file=sys.stderr)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name in ("run", "resume"):
        r = sub.add_parser(name)
        r.add_argument("prompt", nargs="?")
        r.add_argument("--model"); r.add_argument("--effort"); r.add_argument("--sandbox")
        r.add_argument("--cd"); r.add_argument("--add-dir", action="append")
        r.add_argument("--timeout", type=int); r.add_argument("--profile")
        if name == "resume":
            r.add_argument("--session", help="session id; default: most recent")
    c = sub.add_parser("config")
    c.add_argument("action", nargs="?", default="show", choices=["show", "set", "reset"])
    c.add_argument("pairs", nargs="*")
    sub.add_parser("models"); sub.add_parser("status")
    a = ap.parse_args()
    {"config": cmd_config, "models": cmd_models, "status": cmd_status}.get(
        a.cmd, lambda x: run_codex(x, a.cmd == "resume"))(a)


if __name__ == "__main__":
    main()
