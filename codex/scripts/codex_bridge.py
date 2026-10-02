#!/usr/bin/env python3
"""Bridge between Claude Code and the local `codex` CLI.

Subcommands:
  run     [flags] [PROMPT|-]   run a task via `codex exec` (prompt from arg or stdin)
  resume  [flags] [PROMPT|-]   continue the last (or --session ID) Codex session
  config  [show|set k=v ...|reset]   persistent defaults
  roles                        list role presets (use with --role)
  parallel FILE|-              run a JSON list of tasks concurrently
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
    "roles": {},  # user overrides/additions, merged over ROLES below
}

# Role presets: Claude picks the role that fits the job. Each sets sandbox/effort
# (and optionally model) plus a preamble that frames Codex's behaviour.
ROLES = {
    "explorer": {"sandbox": "read-only", "effort": "low",
                 "preamble": "ROLE: explorer. Read-only reconnaissance. Do NOT modify files. Find facts fast and report file paths + line numbers + concise findings."},
    "worker": {"sandbox": "workspace-write", "effort": "medium",
               "preamble": "ROLE: worker. Implement exactly the task described, nothing more. Touch only the files named or clearly required. Run the stated check/test command and report the real result."},
    "debugger": {"sandbox": "workspace-write", "effort": "high",
                 "preamble": "ROLE: debugger. Reproduce first, find the root cause, then make the smallest fix. Report the cause, the fix and proof it works."},
    "reviewer": {"sandbox": "read-only", "effort": "high",
                 "preamble": "ROLE: reviewer. Read-only critical review. Report concrete defects ranked by severity with file:line and a failing scenario. Say so explicitly if you find nothing."},
    "architect": {"sandbox": "read-only", "effort": "xhigh",
                  "preamble": "ROLE: architect. Read-only design analysis. Give a recommendation with trade-offs and a step-by-step plan; do not implement."},
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


def resolve(a, s):
    """Merge precedence: CLI flags > role preset > saved settings."""
    roles = {**ROLES, **s.get("roles", {})}
    role = {}
    if getattr(a, "role", None):
        if a.role not in roles:
            sys.exit(f"unknown role '{a.role}'. roles: {', '.join(roles)}")
        role = roles[a.role]
    g = lambda k: getattr(a, k, None) or role.get(k) or s[k]
    cfg = {k: g(k) for k in ("model", "effort", "sandbox", "timeout", "profile")}
    validate("effort", cfg["effort"]); validate("sandbox", cfg["sandbox"])
    cfg["preamble"] = role.get("preamble", "")
    return cfg


def exec_codex(cfg, prompt, cd=None, add_dir=None, session=None, resume=False):
    """Run codex once. Returns dict(ok, text, thread, error, usage, cfg)."""
    s = load()
    if cfg["preamble"]:
        prompt = cfg["preamble"] + "\n\n" + prompt
    out = tempfile.NamedTemporaryFile(suffix=".txt", delete=False).name
    cmd = [codex_bin(), "exec"]
    if resume:
        cmd.append("resume")
        cmd += [session] if session else ["--last"]
    cmd += ["--json", "--skip-git-repo-check", "-o", out,
            "-m", cfg["model"], "-c", f'model_reasoning_effort="{cfg["effort"]}"']
    if cfg["profile"]:
        cmd += ["-p", cfg["profile"]]
    if not resume:  # `resume` doesn't accept -s/-C/--add-dir; it inherits the session
        cmd += ["-s", cfg["sandbox"], "-C", os.path.abspath(cd or os.getcwd())]
        for d in add_dir or []:
            cmd += ["--add-dir", d]
    cmd += s["extra_args"] + ["-"]
    res = {"ok": False, "text": "", "thread": None, "error": "", "usage": None, "cfg": cfg}
    try:
        p = subprocess.run(cmd, input=prompt, capture_output=True, text=True, timeout=cfg["timeout"])
    except subprocess.TimeoutExpired:
        res["error"] = f"codex timed out after {cfg['timeout']}s"
        return res
    msgs, errors = [], []
    for line in p.stdout.splitlines():
        try:
            ev = json.loads(line)
        except ValueError:
            continue
        t = ev.get("type")
        if t == "thread.started":
            res["thread"] = ev.get("thread_id")
        elif t == "item.completed":
            it = ev.get("item", {})
            if it.get("type") == "agent_message":
                msgs.append(it.get("text", ""))
            elif it.get("type") == "error":
                errors.append(it.get("message", ""))
        elif t in ("error", "turn.failed"):
            errors.append(json.dumps(ev.get("error") or ev.get("message"), ensure_ascii=False))
        elif t == "turn.completed":
            res["usage"] = ev.get("usage")
    try:
        final = open(out).read().strip()
        os.unlink(out)
    except OSError:
        final = ""
    res["text"] = final or (msgs[-1] if msgs else "")
    res["ok"] = p.returncode == 0 and bool(res["text"])
    if not res["ok"]:
        res["error"] = "\n".join(errors) or p.stderr[-2000:] or f"exit {p.returncode}"
    return res


def header(cfg, role=None):
    return (f"[codex] role={role or '-'} model={cfg['model']} effort={cfg['effort']} "
            f"sandbox={cfg['sandbox']}")


def run_codex(a, resume):
    cfg = resolve(a, load())
    prompt = a.prompt
    if prompt in (None, "-"):
        prompt = sys.stdin.read()
    if not prompt.strip():
        sys.exit("empty prompt")
    print(header(cfg, getattr(a, "role", None)) + (" (session inherited)" if resume else ""), file=sys.stderr)
    r = exec_codex(cfg, prompt, a.cd, a.add_dir, getattr(a, "session", None), resume)
    if not r["ok"]:
        sys.exit(f"CODEX FAILED: {r['error']}")
    print(r["text"])
    print(f"\n---\n[codex session={r['thread']} tokens={r['usage']}] follow up: "
          f"codex_bridge.py resume --session {r['thread']} \"<msg>\"", file=sys.stderr)


def cmd_parallel(a):
    """Run many tasks concurrently. Input: JSON list (file or '-'):
    [{"name":"a","role":"explorer","prompt":"...","cd":"/path","model":..,"effort":..,"sandbox":..}]
    Tasks writing to the same files must not run in parallel."""
    from concurrent.futures import ThreadPoolExecutor
    raw = sys.stdin.read() if a.file == "-" else open(a.file).read()
    tasks = json.loads(raw)
    s = load()

    def one(i_t):
        i, t = i_t
        ns = argparse.Namespace(**{k: t.get(k) for k in ("role", "model", "effort", "sandbox", "profile")},
                                timeout=t.get("timeout"))
        cfg = resolve(ns, s)
        r = exec_codex(cfg, t["prompt"], t.get("cd"), t.get("add_dir"))
        return t.get("name") or f"task{i+1}", t.get("role"), r

    with ThreadPoolExecutor(max_workers=min(a.max, len(tasks))) as ex:
        results = list(ex.map(one, enumerate(tasks)))
    failed = 0
    for name, role, r in results:
        print(f"===== {name} {header(r['cfg'], role)} session={r['thread']} =====")
        print(r["text"] if r["ok"] else f"FAILED: {r['error']}")
        failed += not r["ok"]
    sys.exit(1 if failed else 0)


def cmd_roles(_):
    roles = {**ROLES, **load().get("roles", {})}
    for n, r in roles.items():
        print(f"{n:<10} sandbox={r.get('sandbox','-'):<16} effort={r.get('effort','-'):<7} "
              f"model={r.get('model','(default)')}  {r.get('preamble','')[:70]}")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name in ("run", "resume"):
        r = sub.add_parser(name)
        r.add_argument("prompt", nargs="?")
        r.add_argument("--model"); r.add_argument("--effort"); r.add_argument("--sandbox")
        r.add_argument("--role", help="explorer|worker|debugger|reviewer|architect|custom")
        r.add_argument("--cd"); r.add_argument("--add-dir", action="append")
        r.add_argument("--timeout", type=int); r.add_argument("--profile")
        if name == "resume":
            r.add_argument("--session", help="session id; default: most recent")
    c = sub.add_parser("config")
    c.add_argument("action", nargs="?", default="show", choices=["show", "set", "reset"])
    c.add_argument("pairs", nargs="*")
    sub.add_parser("models"); sub.add_parser("status"); sub.add_parser("roles")
    pl = sub.add_parser("parallel"); pl.add_argument("file", help="JSON task list file or -")
    pl.add_argument("--max", type=int, default=4)
    a = ap.parse_args()
    {"config": cmd_config, "models": cmd_models, "status": cmd_status, "roles": cmd_roles, "parallel": cmd_parallel}.get(
        a.cmd, lambda x: run_codex(x, a.cmd == "resume"))(a)


if __name__ == "__main__":
    main()
