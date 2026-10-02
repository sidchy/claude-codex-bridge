#!/usr/bin/env python3
"""Bridge between Claude Code and the local `codex` CLI.

Subcommands:
  run     [flags] [PROMPT|-]   run a task via `codex exec` (prompt from arg or stdin)
  resume  [flags] [PROMPT|-]   continue the last (or --session ID) Codex session
  config  [show|set k=v ...|reset]   persistent defaults
  review [--base B|--commit S] [--adversarial] [focus] [--background]
  watch [id] (live timeline) | log [id] [--full] (transcript) | attach [id] (take over interactively)
  jobs | result [id] | wait [id] | cancel [id]   background job control (run/resume/review --background)
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
    "sandbox": "danger-full-access",  # default: full permission, no confirmations. read-only | workspace-write | danger-full-access
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
    "worker": {"sandbox": "danger-full-access", "effort": "medium",
               "preamble": "ROLE: worker. Implement exactly the task described, nothing more. Touch only the files named or clearly required. Run the stated check/test command and report the real result."},
    "debugger": {"sandbox": "danger-full-access", "effort": "high",
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


def _gitstate(cd):
    try:
        out = subprocess.run(["git", "status", "--porcelain"], cwd=cd, capture_output=True, text=True, timeout=20)
        return set(out.stdout.splitlines()) if out.returncode == 0 else None
    except Exception:
        return None


def exec_codex(cfg, prompt, cd=None, add_dir=None, session=None, resume=False, review=None,
               jid=None, live=False, kind="task"):
    """Run codex once, streaming events to <run>/events.jsonl. Returns dict(ok, text, thread, error,
    usage, cfg, jid, digest). live=True also prints a readable timeline to stderr as it happens."""
    import threading
    s = load()
    cdir = os.path.abspath(cd or os.getcwd())
    if jid is None:
        jid = new_run(kind, cfg, prompt, cdir, review)
    if cfg["preamble"] and prompt:
        prompt = cfg["preamble"] + "\n\n" + prompt
    out = tempfile.NamedTemporaryFile(suffix=".txt", delete=False).name
    cmd = [codex_bin()]
    if cfg["profile"]:  # --profile is a top-level option; exec/review/resume reject it
        cmd += ["-p", cfg["profile"]]
    cmd.append("exec")
    if review is not None:  # native reviewer: target flags only (cannot combine with a prompt)
        cmd.append("review")
    if resume:
        cmd.append("resume")
        cmd += [session] if session else ["--last"]
    cmd += ["--json", "--skip-git-repo-check", "-o", out,
            "-m", cfg["model"], "-c", f'model_reasoning_effort="{cfg["effort"]}"',
            "-c", 'approval_policy="never"']
    if review is not None:
        cmd += review
    elif not resume:  # `resume` doesn't accept -s/-C/--add-dir; it inherits the session
        cmd += ["-s", cfg["sandbox"], "-C", cdir]
        for d in add_dir or []:
            cmd += ["--add-dir", d]
    cmd += s["extra_args"] + (["-"] if review is None else [])
    res = {"ok": False, "text": "", "thread": None, "error": "", "usage": None, "cfg": cfg,
           "jid": jid, "digest": ""}
    before = _gitstate(cdir)
    evf = open(_job_path(jid, "events.jsonl"), "a", buffering=1)
    p = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                         text=True, cwd=cdir)
    timed_out = []
    timer = threading.Timer(cfg["timeout"], lambda: (timed_out.append(1), p.kill()))
    timer.start()
    try:
        p.stdin.write(prompt or ""); p.stdin.close()
    except OSError:
        pass
    msgs, errors, cmds, failed = [], [], 0, 0
    for line in p.stdout:
        try:
            ev = json.loads(line)
        except ValueError:
            continue
        evf.write(line if line.endswith("\n") else line + "\n")
        t = ev.get("type")
        if t == "thread.started":
            res["thread"] = ev.get("thread_id")
            _jwrite(jid, thread=res["thread"])
        elif t == "item.completed":
            it = ev.get("item", {})
            if it.get("type") == "agent_message":
                msgs.append(it.get("text", ""))
            elif it.get("type") == "error":
                errors.append(it.get("message", ""))
            elif it.get("type") == "command_execution":
                cmds += 1; failed += (it.get("exit_code") not in (0, None))
        elif t in ("error", "turn.failed"):
            errors.append(json.dumps(ev.get("error") or ev.get("message"), ensure_ascii=False))
        elif t == "turn.completed":
            res["usage"] = ev.get("usage")
        if live:
            for ln in render_event(ev):
                print(ln, file=sys.stderr, flush=True)
    p.wait(); timer.cancel(); evf.close()
    try:
        final = open(out).read().strip()
        os.unlink(out)
    except OSError:
        final = ""
    res["text"] = final or (msgs[-1] if msgs else "")
    res["ok"] = p.returncode == 0 and bool(res["text"]) and not timed_out
    if not res["ok"]:
        res["error"] = (f"codex timed out after {cfg['timeout']}s" if timed_out
                        else "\n".join(errors) or f"exit {p.returncode}")
    after = _gitstate(cdir)
    changed = sorted(l.strip() for l in (after - before)) if before is not None and after is not None else None
    u = res["usage"] or {}
    parts = [f"{cmds} commands" + (f" ({failed} failed)" if failed else "")]
    if changed is not None:
        parts.append("files changed: " + (", ".join(changed[:15]) + (" ..." if len(changed) > 15 else "") if changed else "none"))
    parts.append(f"tokens in/out {u.get('input_tokens', '?')}/{u.get('output_tokens', '?')}")
    res["digest"] = " · ".join(parts)
    _jwrite(jid, digest=res["digest"], thread=res["thread"])
    return res


def _short(t, n=160):
    t = " ".join(str(t).split())
    return t if len(t) <= n else t[:n - 1] + "…"


def render_event(ev, full=False):
    """Turn one codex JSONL event into human-readable lines ([] = skip)."""
    t = ev.get("type"); it = ev.get("item", {}) or {}; ty = it.get("type")
    if t == "turn.started":
        return ["── turn started ──"]
    if t == "turn.completed":
        u = ev.get("usage", {})
        return [f"── turn done · tokens in/out {u.get('input_tokens','?')}/{u.get('output_tokens','?')} ──"]
    if t in ("error", "turn.failed"):
        return ["✘ " + _short(json.dumps(ev.get("error") or ev.get("message"), ensure_ascii=False), 300)]
    if t == "item.started" and ty == "command_execution":
        c = it.get("command", "")
        c = c.split(" -lc ", 1)[-1].strip("'\"") if " -lc " in c else c
        return ["▶ $ " + (c if full else _short(c, 200))]
    if t == "item.completed":
        if ty == "agent_message":
            return ["💬 " + (it.get("text", "").strip() if full else _short(it.get("text", ""), 400))]
        if ty == "command_execution":
            lines = [f"  {'✓' if it.get('exit_code') == 0 else '✗'} exit {it.get('exit_code')}"]
            o = (it.get("aggregated_output") or "").rstrip().splitlines()
            for l in (o if full else o[:4]):
                lines.append("    " + _short(l, 200 if not full else 2000))
            if not full and len(o) > 4:
                lines.append(f"    … (+{len(o)-4} lines)")
            return lines
        if ty == "reasoning":
            return ["🧠 " + _short(it.get("text", ""), 200)] if it.get("text") else []
        if ty == "error":
            m = it.get("message", "")
            return [] if "ignoring" in m and "configuration setting" in m else ["⚠ " + _short(m, 300)]
        if ty:
            return [f"• {ty}: " + _short(json.dumps({k: v for k, v in it.items() if k not in ('id', 'type')}, ensure_ascii=False), 300)]
    return []


def header(cfg, role=None):
    return (f"[codex] role={role or '-'} model={cfg['model']} effort={cfg['effort']} "
            f"sandbox={cfg['sandbox']}")


def print_footer(r):
    me = os.path.abspath(__file__)
    print(f"\n--- Codex activity: {r['digest']}\n    run {r['jid']} · session {r['thread']}"
          f"\n    transcript: {me} log {r['jid']}\n    take over : {me} attach {r['jid']}")


def run_codex(a, resume):
    cfg = resolve(a, load())
    prompt = a.prompt
    if prompt in (None, "-"):
        prompt = sys.stdin.read()
    if not prompt.strip():
        sys.exit("empty prompt")
    print(header(cfg, getattr(a, "role", None)) + (" (session inherited)" if resume else ""), file=sys.stderr)
    if a.background:
        return start_job("resume" if resume else "task", cfg, prompt, a.cd, a.add_dir, resume, getattr(a, "session", None))
    r = exec_codex(cfg, prompt, a.cd, a.add_dir, getattr(a, "session", None), resume, live=True,
                   kind="resume" if resume else "task")
    finish_run(r)
    if not r["ok"]:
        sys.exit(f"CODEX FAILED: {r['error']}\n(run {r['jid']}; transcript: codex_bridge.py log {r['jid']})")
    print(r["text"])
    print_footer(r)


JOBS = os.path.expanduser("~/.claude/codex-bridge/jobs")
ADVERSARIAL = ("You are performing an ADVERSARIAL review. Your job is to break confidence in the change, not to "
               "validate it: assume it can fail in subtle, costly ways. Hunt for auth/trust-boundary gaps, data loss, "
               "rollback/retry/idempotency holes, races, empty/null/timeout behaviour, version/schema drift, and "
               "observability gaps. Challenge the approach and design choices too, not only defects. Report only "
               "material findings, ranked by severity, each with file:line, a concrete failure scenario and a fix. "
               "No style nits. If nothing material survives, say so. Do not modify any files.\n\n")


def _job_path(jid, name):
    return os.path.join(JOBS, jid, name)


def _jwrite(jid, **kw):
    import fcntl
    path = _job_path(jid, "state.json")
    with open(_job_path(jid, "state.lock"), "w") as lk:
        fcntl.flock(lk, fcntl.LOCK_EX)  # serialize read-modify-write across processes
        st = {}
        try:
            st = json.load(open(path))
        except (OSError, ValueError):
            pass
        st.update(kw)
        tmp = path + ".tmp"
        json.dump(st, open(tmp, "w"), ensure_ascii=False)
        os.replace(tmp, path)  # atomic publish: readers never see a partial file


def _jread(jid):
    try:
        return json.load(open(_job_path(jid, "state.json")))
    except (OSError, ValueError):
        return None


def _alive(pid):
    try:
        os.kill(pid, 0)
        return True
    except (OSError, TypeError):
        return False


def new_run(kind, cfg, prompt, cd, review=None, spec=None):
    import time, uuid
    jid = time.strftime("%m%d-%H%M%S-") + uuid.uuid4().hex[:4]
    os.makedirs(os.path.join(JOBS, jid))
    if spec:
        json.dump(spec, open(_job_path(jid, "spec.json"), "w"), ensure_ascii=False)
    _jwrite(jid, id=jid, kind=kind, status="running", cwd=cd, model=cfg["model"], effort=cfg["effort"],
            started=time.time(), pid=os.getpid(),
            title=(prompt or " ".join(review or []))[:70].replace("\n", " "))
    return jid


def finish_run(r):
    import time
    st = _jread(r["jid"]) or {}
    if st.get("status") == "cancelled":
        return
    open(_job_path(r["jid"], "result.txt"), "w").write(r["text"] if r["ok"] else "FAILED: " + r["error"])
    _jwrite(r["jid"], status="done" if r["ok"] else "failed", usage=r["usage"], finished=time.time())


def start_job(kind, cfg, prompt, cd, add_dir=None, resume=False, session=None, review=None):
    cd = os.path.abspath(cd or os.getcwd())
    jid = new_run(kind, cfg, prompt, cd, review,
                  spec={"cfg": cfg, "prompt": prompt, "cd": cd, "add_dir": add_dir,
                        "resume": resume, "session": session, "review": review})
    log = open(_job_path(jid, "log.txt"), "w")
    pr = subprocess.Popen([sys.executable, os.path.abspath(__file__), "_job", jid],
                          stdout=log, stderr=log, stdin=subprocess.DEVNULL, start_new_session=True)
    _jwrite(jid, pid=pr.pid)
    print(f"started background job {jid} ({kind}, {cfg['model']} {cfg['effort']}).\n"
          f"  watch live : {os.path.abspath(__file__)} watch {jid}\n"
          f"  other      : jobs | log {jid} | result {jid} | wait {jid} | cancel {jid} | attach {jid}")


def cmd__job(a):
    jid = a.jid
    spec = json.load(open(_job_path(jid, "spec.json")))
    r = exec_codex(spec["cfg"], spec["prompt"], spec["cd"], spec["add_dir"], spec["session"],
                   spec["resume"], spec["review"], jid=jid)
    finish_run(r)


def _resolve_job(ref):
    if not os.path.isdir(JOBS):
        sys.exit("no jobs yet")
    ids = sorted(os.listdir(JOBS))
    if ref in (None, "last"):
        if not ids:
            sys.exit("no jobs yet")
        return ids[-1]
    m = [i for i in ids if i == ref or i.endswith(ref)]
    if len(m) != 1:
        sys.exit(f"job '{ref}' not found or ambiguous")
    return m[0]


def _refresh(jid):
    st = _jread(jid) or {}
    if st.get("status") == "running" and not _alive(st.get("pid")):
        _jwrite(jid, status="failed", error="worker process died")
        st = _jread(jid)
    return st


def cmd_jobs(a):
    import time
    ids = sorted(os.listdir(JOBS), reverse=True)[:a.limit] if os.path.isdir(JOBS) else []
    if not ids:
        print("no jobs"); return
    print(f"{'ID':<17}{'KIND':<8}{'STATUS':<10}{'AGE':<7}{'MODEL/EFFORT':<22}TASK")
    for jid in ids:
        st = _refresh(jid)
        age = f"{int((time.time()-st.get('started', time.time()))/60)}m"
        print(f"{jid:<17}{st.get('kind',''):<8}{st.get('status',''):<10}{age:<7}"
              f"{st.get('model','')+' '+st.get('effort',''):<22}{st.get('title','')}")


def cmd_result(a):
    jid = _resolve_job(a.job); st = _refresh(jid)
    if st["status"] == "running":
        print(f"job {jid} still running"); return
    print(open(_job_path(jid, "result.txt")).read() if os.path.exists(_job_path(jid, "result.txt"))
          else f"job {jid}: {st['status']} ({st.get('error','no output')})")
    print(f"\n[job {jid} {st['status']} session={st.get('thread')}]", file=sys.stderr)


def cmd_wait(a):
    import time
    jid = _resolve_job(a.job); t0 = time.time()
    while _refresh(jid)["status"] == "running":
        if time.time() - t0 > a.timeout:
            print(f"job {jid} still running after {a.timeout}s"); return
        time.sleep(3)
    cmd_result(argparse.Namespace(job=jid))


def cmd_cancel(a):
    import signal
    jid = _resolve_job(a.job); st = _refresh(jid)
    if st["status"] != "running":
        print(f"job {jid} is {st['status']}"); return
    _jwrite(jid, status="cancelled")
    try:
        os.killpg(st["pid"], signal.SIGTERM)
    except OSError:
        pass
    print(f"cancelled {jid}")


def _stream(jid, follow, full=False):
    import time
    path = _job_path(jid, "events.jsonl")
    for _ in range(100):  # wait for the log file to appear
        if os.path.exists(path): break
        time.sleep(0.2)
    else:
        sys.exit(f"no event log for {jid}")
    st = _jread(jid) or {}
    print(f"═ codex run {jid} · {st.get('model')} {st.get('effort')} · {st.get('cwd')}\n═ {st.get('title','')}", flush=True)
    with open(path) as f:
        while True:
            line = f.readline()
            if line:
                try:
                    for ln in render_event(json.loads(line), full):
                        print(ln, flush=True)
                except ValueError:
                    pass
                continue
            if not follow or _refresh(jid).get("status") != "running":
                break
            time.sleep(0.5)
    st = _refresh(jid)
    print(f"═ {st.get('status')} · {st.get('digest','')}", flush=True)


def cmd_watch(a):
    _stream(_resolve_job(a.job), follow=True)


def cmd_log(a):
    _stream(_resolve_job(a.job), follow=False, full=a.full)


def cmd_attach(a):
    jid = _resolve_job(a.job); st = _jread(jid) or {}
    if not st.get("thread"):
        sys.exit("no codex session id recorded yet for this run")
    print(f"cd {st['cwd']} && {codex_bin()} resume {st['thread']}")
    print("# Run the line above in a terminal to continue this exact Codex session interactively (you drive).", file=sys.stderr)


def cmd_review(a):
    cfg = resolve(a, load())
    cfg["sandbox"] = "read-only"
    focus = " ".join(a.focus).strip()
    if a.commit:
        target, tflags, desc = "the changes introduced by commit " + a.commit, ["--commit", a.commit], f"`git show {a.commit}`"
    elif a.base:
        target, tflags, desc = f"the branch diff against {a.base}", ["--base", a.base], f"`git diff {a.base}...HEAD`"
    else:
        target, tflags, desc = "all uncommitted changes (staged, unstaged, untracked)", ["--uncommitted"], \
            "`git status`, `git diff HEAD`, and the contents of untracked files"
    if a.adversarial or focus:  # native reviewer can't take a prompt -> drive it as a read-only exec
        prompt = (ADVERSARIAL if a.adversarial else "Review the change critically. Report concrete defects ranked by severity with file:line. Do not modify files.\n\n") \
            + f"Target: {target}. Inspect it with {desc}.\nFocus: {focus or '(none; cover everything material)'}"
        review = None
    else:
        prompt, review = None, tflags
    print(header(cfg, "reviewer") + f" target={target}", file=sys.stderr)
    if a.background:
        return start_job("review", cfg, prompt, a.cd, review=review)
    r = exec_codex(cfg, prompt, a.cd, review=review, live=True, kind="review")
    finish_run(r)
    if not r["ok"]:
        sys.exit(f"CODEX FAILED: {r['error']}")
    print(r["text"])
    print_footer(r)


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
        r = exec_codex(cfg, t["prompt"], t.get("cd"), t.get("add_dir"), kind="task")
        finish_run(r)
        return t.get("name") or f"task{i+1}", t.get("role"), r

    with ThreadPoolExecutor(max_workers=min(a.max, len(tasks))) as ex:
        results = list(ex.map(one, enumerate(tasks)))
    failed = 0
    for name, role, r in results:
        print(f"===== {name} {header(r['cfg'], role)} session={r['thread']} =====")
        print(r["text"] if r["ok"] else f"FAILED: {r['error']}")
        print(f"  [activity: {r['digest']} · run {r['jid']}]")
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
        r.add_argument("--background", action="store_true", help="return a job id immediately")
        if name == "resume":
            r.add_argument("--session", help="session id; default: most recent")
    c = sub.add_parser("config")
    c.add_argument("action", nargs="?", default="show", choices=["show", "set", "reset"])
    c.add_argument("pairs", nargs="*")
    sub.add_parser("models"); sub.add_parser("status"); sub.add_parser("roles")
    rv = sub.add_parser("review"); rv.add_argument("focus", nargs="*")
    g = rv.add_mutually_exclusive_group(); g.add_argument("--base"); g.add_argument("--commit")
    rv.add_argument("--adversarial", action="store_true"); rv.add_argument("--background", action="store_true")
    for x in ("model", "effort", "cd", "timeout", "profile"):
        rv.add_argument("--" + x, type=int if x == "timeout" else None)
    jb = sub.add_parser("jobs"); jb.add_argument("--limit", type=int, default=15)
    for n in ("watch", "log", "attach"):
        q = sub.add_parser(n); q.add_argument("job", nargs="?", default="last")
        if n == "log": q.add_argument("--full", action="store_true")
    for n in ("result", "cancel", "wait"):
        q = sub.add_parser(n); q.add_argument("job", nargs="?", default="last")
        if n == "wait": q.add_argument("--timeout", type=int, default=600)
    sub.add_parser("_job").add_argument("jid")
    pl = sub.add_parser("parallel"); pl.add_argument("file", help="JSON task list file or -")
    pl.add_argument("--max", type=int, default=4)
    a = ap.parse_args()
    {"config": cmd_config, "models": cmd_models, "status": cmd_status, "roles": cmd_roles, "parallel": cmd_parallel,
        "review": cmd_review, "watch": cmd_watch, "log": cmd_log, "attach": cmd_attach, "jobs": cmd_jobs, "result": cmd_result, "wait": cmd_wait, "cancel": cmd_cancel, "_job": cmd__job}.get(
        a.cmd, lambda x: run_codex(x, a.cmd == "resume"))(a)


if __name__ == "__main__":
    main()
