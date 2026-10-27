#!/usr/bin/env python3
"""Bridge between Claude Code and the local `codex` CLI.

Subcommands:
  run     [flags] [PROMPT|-]   run a task via `codex exec` (prompt from arg or stdin)
  resume  [flags] [PROMPT|-]   continue the last (or --session ID) Codex session
  steer [job] [--name LABEL] [--foreground] MESSAGE|-
          safely interrupt the current step and resume the SAME session with guidance
  config  [show|set k=v ...|reset]   persistent defaults
  review [--base B|--commit S] [--adversarial] [focus] [--background]
  watch [id] (live timeline) | log [id] [--full] (transcript) | attach [id] (take over interactively)
  jobs | result [id] | wait [id] | cancel [id]   background job control (run/resume/review --background)
  roles                        list role presets (use with --role)
  parallel FILE|-              run a JSON list of tasks concurrently
  models                       list models/efforts from Codex's local cache
  status                       codex version + effective defaults

Output: verbatim final report; --live opts into foreground stderr timelines.
log defaults to a compact execution trace; log --full and watch show detail.
result/wait --full prints only the saved report.
run/resume/steer --subagents none|auto|active --max-subagents 1..20 controls prompt-level dispatch.

Persistent settings: ~/.claude/codex-bridge/settings.json
Per-call flags override them: --model --effort --sandbox --cd --add-dir --timeout --profile
"""
import argparse, glob, json, os, shutil, subprocess, sys, tempfile
import contextlib, re, signal, time, hashlib, shlex, selectors, math
if os.name == "nt":
    import importlib.util
    import queue, threading, msvcrt
else:
    import fcntl
from datetime import datetime, timezone
from pathlib import Path
from collections import deque

if os.name == "nt":
    _spec = importlib.util.spec_from_file_location("_codex_bridge_windows", Path(__file__).with_name("_windows.py"))
    win = importlib.util.module_from_spec(_spec)
    _spec.loader.exec_module(win)
    for _stream in (sys.stdin, sys.stdout, sys.stderr):
        if hasattr(_stream, "reconfigure"):
            _stream.reconfigure(encoding="utf-8", **({"newline": ""} if _stream is not sys.stdin else {}))


def replace_file(source, destination):
    """Windows readers/virus scanners can temporarily deny delete sharing."""
    if os.name != "nt":
        return os.replace(source, destination)
    deadline = time.monotonic() + 2.0
    while True:
        try:
            return os.replace(source, destination)
        except PermissionError:
            if time.monotonic() >= deadline:
                raise
            time.sleep(0.02)


def read_owned(path, newline=None):
    deadline = time.monotonic() + 2.0
    while True:
        try:
            with open(path, encoding="utf-8", newline=newline) as source:
                return source.read()
        except PermissionError:
            if os.name != "nt" or time.monotonic() >= deadline:
                raise
            time.sleep(0.02)


def cli_command(binary, *args):
    if os.name == "nt" and str(binary).lower().endswith(".py"):
        return [sys.executable, "-X", "utf8", str(binary), *args]
    return [binary, *args]


def launch_options(suspended=False):
    return {"creationflags": subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP | (4 if suspended else 0)} if os.name == "nt" else {"start_new_session": True}


def write_report(path, text):
    with open(path, "w", encoding="utf-8", newline="") as stream:
        stream.write(text)


def powershell_command(argv):
    # Windows PowerShell 5.1 strips embedded double quotes when forwarding to
    # native programs. Pass such argv through Python's lossless subprocess API;
    # the transport code and payload contain no double quotes themselves. This
    # also works with PowerShell 7's different native argument passing modes.
    if any('"' in str(arg) or "\n" in str(arg) or "\r" in str(arg) for arg in argv):
        import base64
        payload = base64.b64encode(json.dumps(list(argv), ensure_ascii=True).encode("ascii")).decode("ascii")
        code = "import base64,json,subprocess;raise SystemExit(subprocess.call(json.loads(base64.b64decode('" + payload + "'))))"
        argv = [sys.executable, "-X", "utf8", "-c", code]
    return "& " + " ".join("'" + str(arg).replace("'", "''") + "'" for arg in argv)


BRIDGE_HOME = os.path.realpath(os.path.expanduser(os.environ.get("CODEX_BRIDGE_HOME", "~/.claude/codex-bridge")))
SETTINGS = os.path.join(BRIDGE_HOME, "settings.json")
DEFAULTS = {
    "model": "gpt-6.1-sol",
    "effort": "medium",
    "sandbox": "danger-full-access",  # default: full permission, no confirmations. read-only | workspace-write | danger-full-access
    "timeout": 1800,
    "profile": "",
    "codex_bin": "auto",  # auto = newest of ChatGPT.app bundled CLI and PATH; or "path", or a binary path
    "extra_args": [],
    "model_reasoning": "gpt-6-astra",  # hard reasoning: architect, escalated debugging, judging quality
    "model_bulk": "gpt-6-luna",        # highly repetitive simple work (sub-agent fan-out)
    "review_model": "gpt-6-astra",     # reviews default to this model + effort
    "review_effort": "high",
    "team_policy": True,               # tell Codex how/when to spawn sub-agents and with which models
    "max_parallel": 4,
    "subagents": "auto",
    "max_subagents": 20,
    "codex_memory": "scoped",  # scoped = bind Codex's global memory to this workspace; off = no memory read/write; on = native
    "roles": {},  # user overrides/additions, merged over ROLES below
}

# Role presets: Claude picks the role that fits the job. Each sets sandbox plus a preamble
# (effort/model follow the saved config unless a role or flag sets them explicitly) that frames Codex's behaviour.
ROLES = {
    "explorer": {"sandbox": "read-only",
                 "preamble": "ROLE: explorer. Read-only reconnaissance. Do NOT modify files. Find facts fast and report file paths + line numbers + concise findings."},
    "worker": {
                 "preamble": "ROLE: worker. Implement exactly the task described, nothing more. Touch only the files named or clearly required. Run the stated check/test command and report the real result."},
    "debugger": {
                 "preamble": "ROLE: debugger. Reproduce first, find the root cause, then make the smallest fix. Report the cause, the fix and proof it works."},
    "reviewer": {"sandbox": "read-only",
                 "preamble": "ROLE: reviewer. Read-only critical review. Report concrete defects ranked by severity with file:line and a failing scenario. Say so explicitly if you find nothing."},
    "architect": {"sandbox": "read-only",
                 "preamble": "ROLE: architect. Read-only design analysis. Give a recommendation with trade-offs and a step-by-step plan; do not implement."},
}
EFFORTS = {"low", "medium", "high", "xhigh", "max", "ultra"}
SANDBOXES = {"read-only", "workspace-write", "danger-full-access"}


@contextlib.contextmanager
def file_lock(path):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "a", encoding="utf-8") as lock:
        if os.name != "nt":
            fcntl.flock(lock, fcntl.LOCK_EX)
            yield
            return
        lock.seek(0, os.SEEK_END)
        if not lock.tell():
            lock.write("0")
            lock.flush()
        while True:
            lock.seek(0)
            try:
                msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
                break
            except OSError as exc:
                if exc.errno not in (13, 36):
                    raise
                time.sleep(0.02)
        try:
            yield
        finally:
            lock.seek(0)
            msvcrt.locking(lock.fileno(), msvcrt.LK_UNLCK, 1)


def atomic_json(path, value):
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path), prefix=".bridge-")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(value, f, indent=2, ensure_ascii=False)
            f.flush()
            os.fsync(f.fileno())
        replace_file(tmp, path)
    finally:
        Path(tmp).unlink(missing_ok=True)


def _load_settings():
    try:
        stored = json.loads(read_owned(SETTINGS))
        if not isinstance(stored, dict):
            raise ValueError("expected a JSON object")
    except FileNotFoundError:
        if os.path.lexists(SETTINGS):
            sys.exit(f"cannot read settings {SETTINGS}: broken symlink")
        stored = {}
    except (OSError, ValueError) as exc:
        sys.exit(f"cannot read settings {SETTINGS}: {exc}")
    try:
        stored.pop("brief_chars", None)  # Removed setting in older bridge installations.
        stored = {k: validate(k, v) for k, v in stored.items()}
    except SystemExit as exc:
        sys.exit(f"cannot read settings {SETTINGS}: {exc}")
    return {**DEFAULTS, **stored}


def load():
    with file_lock(SETTINGS + ".lock"):
        return _load_settings()


def save(s):
    with file_lock(SETTINGS + ".lock"):
        atomic_json(SETTINGS, s)


def validate(k, v):
    def fail(message):
        sys.exit(f"invalid {k}: {message}")

    if k == "brief_chars":
        fail("setting was removed; final reports are no longer truncated")
    if k not in DEFAULTS:
        fail(f"unknown key; keys: {', '.join(DEFAULTS)}")
    if k in ("effort", "review_effort"):
        if not isinstance(v, str) or v not in EFFORTS:
            fail(f"must be one of {sorted(EFFORTS)}")
    elif k == "sandbox":
        if not isinstance(v, str) or v not in SANDBOXES:
            fail(f"must be one of {sorted(SANDBOXES)}")
    elif k in ("timeout", "max_parallel", "max_subagents"):
        try:
            if isinstance(v, bool) or not isinstance(v, (int, str)):
                raise ValueError()
            value = int(v)
            if value < 1:
                raise ValueError()
            if k == "max_subagents" and value > 20:
                fail("must be an integer from 1 to 20")
            return value
        except (ValueError, TypeError):
            fail("must be an integer from 1 to 20" if k == "max_subagents" else "must be a positive integer")
    elif k == "subagents":
        if not isinstance(v, str) or v not in ("none", "auto", "active"):
            fail("must be one of none, auto, active")
    elif k == "codex_memory":
        if not isinstance(v, str) or v not in ("scoped", "off", "on"):
            fail("must be one of ['off', 'on', 'scoped']")
    elif k == "team_policy":
        if isinstance(v, bool):
            return v
        if str(v).lower() not in ("1", "true", "yes", "on", "0", "false", "no", "off"):
            fail("must be true or false")
        return str(v).lower() in ("1", "true", "yes", "on")
    elif k == "roles":
        if isinstance(v, str):
            try:
                v = json.loads(v)
            except ValueError as exc:
                fail(f"expected a JSON object of objects: {exc}")
        if not isinstance(v, dict):
            fail("expected a JSON object of objects")
        for name, role in v.items():
            if not isinstance(name, str) or not name or not isinstance(role, dict):
                fail("expected nonempty role names mapped to objects")
            for field, value in role.items():
                if field in ("sandbox", "effort"):
                    validate(field, value)
                elif field in ("model", "preamble"):
                    if not isinstance(value, str):
                        fail(f"{name}.{field} must be a string")
                else:
                    fail(f"unknown field {name}.{field}; use sandbox, effort, model, preamble")
    elif k == "extra_args":
        if isinstance(v, str):
            try:
                v = json.loads(v) if v.lstrip().startswith("[") else (win.split_args(v) if os.name == "nt" else shlex.split(v))
            except ValueError as exc:
                fail(f"expected JSON array or shell argument string: {exc}")
        if not isinstance(v, list) or any(not isinstance(arg, str) for arg in v):
            fail("expected an array of strings")
    elif not isinstance(v, str):
        fail("must be a string")
    return v


GUI_BIN = "/Applications/ChatGPT.app/Contents/Resources/codex-cli/CodexCLI.app/Contents/MacOS/codex"


def _ver(b):
    try:
        out = subprocess.run(cli_command(b, "--version"), capture_output=True, text=True, timeout=10, encoding="utf-8").stdout
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
    with file_lock(SETTINGS + ".lock"):
        if a.action == "reset":
            backup = None
            try:
                _load_settings()
            except SystemExit:
                backup = SETTINGS + ".corrupt-" + str(time.time_ns())
                try:
                    replace_file(SETTINGS, backup)
                except OSError as exc:
                    sys.exit(f"cannot back up corrupt settings {SETTINGS}: {exc}")
            s = dict(DEFAULTS)
            try:
                atomic_json(SETTINGS, s)
            except OSError:
                if backup:
                    replace_file(backup, SETTINGS)
                raise
            if backup:
                print(f"preserved corrupt settings: {backup}", file=sys.stderr)
            print("reset to defaults")
        else:
            s = _load_settings()
            if a.action == "set":
                for kv in a.pairs:
                    if "=" not in kv:
                        sys.exit(f"expected key=value, got {kv}")
                    k, v = kv.split("=", 1)
                    s[k] = validate(k, v)
                atomic_json(SETTINGS, s)
    print(json.dumps(s, indent=2, ensure_ascii=False))


def model_catalog(cfg=None, cwd=None):
    settings = load()
    cfg = cfg or settings
    binary = codex_bin()
    profile = cfg.get("profile", "")
    extra = cfg.get("extra_args", settings["extra_args"])
    context = {"profile": profile, "extra_args": extra,
               "cwd": os.path.realpath(cwd or os.getcwd()),
               "codex_home": os.path.expanduser(os.environ.get("CODEX_HOME", "~/.codex"))}
    command = cli_command(binary) + (["-p", profile] if profile else []) + ["debug", "models"]
    # Only config overrides apply to catalog lookup; exec-only arguments (e.g.
    # --add-dir) still participate in the cache key and custom-model exception.
    for index, arg in enumerate(extra):
        if arg in ("-c", "--config") and index + 1 < len(extra):
            command += [arg, extra[index + 1]]
        elif arg.startswith("--config=") or (arg.startswith("-c") and len(arg) > 2):
            command.append(arg)
    path = os.path.join(BRIDGE_HOME, "models.json")
    with file_lock(path + ".lock"):
        try:
            cached = json.loads(read_owned(path))
            if cached.get("binary") == binary and cached.get("context") == context and 0 <= time.time() - cached["fetched"] < 3600:
                return cached["models"]
        except (OSError, ValueError, KeyError, TypeError):
            pass
        try:
            out = subprocess.run(command, cwd=context["cwd"], capture_output=True, text=True, timeout=30, encoding="utf-8")
            if out.returncode:
                raise ValueError(out.stderr.strip() or f"exit {out.returncode}")
            models = [{k: m.get(k) for k in ("slug", "visibility", "default_reasoning_level", "supported_reasoning_levels")}
                      for m in json.loads(out.stdout)["models"]]
            if not models or any(not m["slug"] or not isinstance(m["supported_reasoning_levels"], list) for m in models):
                raise ValueError("empty or invalid model catalog")
        except (OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError) as exc:
            sys.exit(f"cannot read model catalog via `codex debug models`: {exc}")
        atomic_json(path, {"binary": binary, "context": context, "fetched": time.time(), "models": models})
        return models


def custom_model_context(cfg, settings):
    if cfg.get("profile") or cfg.get("extra_args", settings["extra_args"]) or cfg.get("model_provider"):
        return True
    native = Path(os.path.expanduser(os.environ.get("CODEX_HOME", "~/.codex"))) / "config.toml"
    try:
        text = native.read_text(encoding="utf-8")
    except OSError:
        return False
    try:
        import tomllib  # Python 3.11+
        provider = tomllib.loads(text).get("model_provider", "openai")
    except ImportError:  # python3 may be 3.9: read the top-level key without a TOML parser
        top = text.split("\n[", 1)[0]
        m = re.search(r'^\s*model_provider\s*=\s*["\']([^"\']+)["\']', top, re.M)
        provider = m.group(1) if m else "openai"
    except ValueError:
        return False
    return provider != "openai"


def validate_model(cfg, cwd=None):
    custom = custom_model_context(cfg, load())
    try:
        catalog = model_catalog(cfg, cwd)
    except SystemExit:
        if not custom:
            raise
        # Keep validating known models even if a custom profile's lookup fails.
        try:
            catalog = model_catalog({"profile": "", "extra_args": []}, cwd)
        except SystemExit:
            catalog = []
    models = {m["slug"]: m for m in catalog}
    if cfg["model"] not in models:
        if custom:
            print(f"[codex] WARNING: model {cfg['model']!r} is not in the catalog; "
                  "allowing it for the configured profile/provider/extra_args", file=sys.stderr)
            return
        sys.exit(f"unknown model {cfg['model']!r}; available: {', '.join(sorted(models))}; run `models`")
    efforts = [level["effort"] for level in models[cfg["model"]]["supported_reasoning_levels"]]
    if cfg["effort"] not in efforts:
        sys.exit(f"model {cfg['model']} does not support effort {cfg['effort']!r}; supported: {', '.join(efforts)}")


def cmd_models(_):
    for m in model_catalog():
        if m.get("visibility") != "list":
            continue
        lv = [r.get("effort") for r in m.get("supported_reasoning_levels", [])]
        print(f"{m['slug']:<16} default={m.get('default_reasoning_level')}  efforts={','.join(lv)}")


def cmd_status(a):
    if getattr(a, "json", False) or getattr(a, "job", "last") != "last" or getattr(a, "name", None):
        print(json.dumps(compact_state(_refresh(_resolve_job(a.job, a.name))), separators=(",", ":")))
        return
    print(subprocess.run(cli_command(codex_bin(), "--version"), capture_output=True, text=True, encoding="utf-8").stdout.strip())
    print("settings file:", SETTINGS)
    print(json.dumps(load(), indent=2, ensure_ascii=False))


def team_policy(s):
    return (
        "\n\nTEAM POLICY (you are the lead; sub-agents are OPTIONAL, use them only when work splits into independent "
        "parts with DISJOINT files/inputs; never for small tasks). Pick the model per sub-agent when you spawn it:\n"
        f"- Highly repetitive, simple per-item work (bulk edits, per-file/per-record transforms, format conversion, "
        f"extraction, mechanical checks): fan out `{s['model_bulk']}` sub-agents, up to {s['max_subagents']} running concurrently "
        f"(it handles high concurrency well), reasoning effort high (xhigh only if items are subtle). Use it when "
        f"there are roughly 10+ independent items: one sub-agent per item or per small shard, disjoint outputs; for "
        f"large batches use many shards (up to the {s['max_subagents']} concurrent) to cut latency; for fewer items just do the work yourself. Spot-check a sample of their outputs before reporting.\n"
        f"- Work that needs real intelligence (design, tricky logic, hard debugging, judging quality or ambiguity): "
        f"`{s['model_reasoning']}`, effort medium (high if genuinely hard).\n"
        f"- Everything else: do it yourself, or `{s['model']}` at medium.\n"
        "Give each sub-agent a self-contained brief (goal, inputs, output location, done-criteria). You verify their "
        "output before reporting, and your final reply must list which sub-agents/models/efforts you used.")


def resolve(a, s):
    """Merge precedence: CLI flags > role preset > saved settings."""
    roles = {**ROLES, **s.get("roles", {})}
    role = {}
    if getattr(a, "role", None):
        if a.role not in roles:
            sys.exit(f"unknown role '{a.role}'. roles: {', '.join(roles)}")
        role = roles[a.role]
    rname = getattr(a, "role", None)
    # built-in role defaults that come from settings (flags still win over everything)
    sdef = {"reviewer": {"model": s["review_model"], "effort": s["review_effort"]},
            "architect": {"model": s["model_reasoning"]}}.get(rname, {})
    g = lambda k: next(v for v in (getattr(a, k, None), role.get(k), sdef.get(k), s[k]) if v is not None)
    cfg = {k: g(k) for k in ("model", "effort", "sandbox", "timeout", "profile", "subagents", "max_subagents")}
    if rname in ("explorer", "reviewer", "architect"):
        cfg["sandbox"] = getattr(a, "sandbox", None) or "read-only"
    validate("effort", cfg["effort"]); validate("sandbox", cfg["sandbox"])
    cfg["timeout"] = validate("timeout", cfg["timeout"])
    for key in ("subagents", "max_subagents"):
        cfg[key] = validate(key, cfg[key])
    cfg["preamble"] = role.get("preamble", "")
    if cfg["subagents"] == "auto" and s.get("team_policy") and rname in ("worker", "debugger", "architect"):
        cfg["preamble"] += team_policy({**s, "max_subagents": cfg["max_subagents"]})
    return cfg


HASH_FILE_LIMIT = 200
HASH_BYTE_LIMIT = 32 * 1024 * 1024


def _gitstate(cd, include=()):
    try:
        root = subprocess.run(["git", "rev-parse", "--show-toplevel"], cwd=cd,
                              capture_output=True, text=True, timeout=20, encoding="utf-8")
        if root.returncode:
            return None
        out = subprocess.run(["git", "status", "--porcelain", "-z", "--untracked-files=all"],
                             cwd=cd, capture_output=True, timeout=20)
        if out.returncode:
            return None
        paths = set(include)
        records = iter(out.stdout.split(b"\0"))
        for record in records:
            if not record:
                continue
            paths.add(os.fsdecode(record[3:]))
            if b"R" in record[:2] or b"C" in record[:2]:
                paths.add(os.fsdecode(next(records)))
        hashes, used = {}, 0
        capped = len(paths) > HASH_FILE_LIMIT
        for name in sorted(paths)[:HASH_FILE_LIMIT]:
            path = Path(root.stdout.strip()) / name
            try:
                if path.is_symlink():
                    hashes[name] = hashlib.sha256(os.fsencode(os.readlink(path))).hexdigest()
                elif not path.exists():
                    hashes[name] = "missing"
                elif path.is_file():
                    size = path.stat().st_size
                    if used + size > HASH_BYTE_LIMIT:
                        capped = True
                        continue
                    used += size
                    digest = hashlib.sha256()
                    with path.open("rb") as f:
                        for chunk in iter(lambda: f.read(65536), b""):
                            digest.update(chunk)
                    hashes[name] = digest.hexdigest()
            except OSError:
                capped = True
        return {"hashes": hashes, "capped": capped}
    except (OSError, subprocess.SubprocessError, StopIteration):
        return None


def memory_workspaces():
    """Workspace paths that Codex's global MEMORY.md has entries for (from its `applies_to: cwd=` lines)."""
    path = os.path.join(_codex_home(), "memories", "MEMORY.md")
    found = []
    try:
        with open(path, encoding="utf-8", errors="ignore") as source:
            for line in source:
                m = re.match(r"\s*applies_to:\s*cwd=([^;]+)", line)
                if m:
                    separator = r",\s*(?=[A-Za-z]:[/\\]|[/\\]{2}|/)" if os.name == "nt" else r",\s*(?=/)"
                    for item in re.split(separator, m.group(1)):
                        item = item.strip()
                        if not (os.path.isabs(item) if os.name == "nt" else item.startswith("/")) or "{" in item or "}" in item or "\x00" in item:  # skip brace patterns / junk
                            continue
                        try:
                            item = os.path.realpath(item)
                        except (OSError, ValueError):
                            continue
                        if item not in found:
                            found.append(item)
    except OSError:
        pass
    return found


def memory_scope(cdir, known=None):
    """Bind entries to this Git root (including its main checkout), never an outer repo.
    Outside Git, only an exact directory match is meaningful."""
    known = memory_workspaces() if known is None else known
    cdir = os.path.realpath(cdir)
    root, main = None, None
    try:
        top = subprocess.run(["git", "rev-parse", "--show-toplevel"], cwd=cdir,
                             capture_output=True, text=True, timeout=3, check=True, encoding="utf-8")
        root = os.path.realpath(top.stdout.strip())
        common = subprocess.run(["git", "rev-parse", "--git-common-dir"], cwd=cdir,
                                capture_output=True, text=True, timeout=3, check=True, encoding="utf-8")
        common = os.path.realpath(os.path.join(cdir, common.stdout.strip()))
        if os.path.basename(common) == ".git":
            main = os.path.dirname(common)
    except (OSError, ValueError, subprocess.SubprocessError):
        root, main = None, None
    mine, others = [], []
    for w in known:
        try:
            w = os.path.realpath(w)
            inside_root = root and os.path.commonpath([root, w]) == root
            ancestor = os.path.commonpath([cdir, w]) == w
            belongs = w == cdir or w in (root, main) or (inside_root and ancestor)
        except (OSError, ValueError, TypeError):
            continue
        (mine if belongs else others).append(w)
    return mine, others


def memory_guard(cdir, known=None):
    """Codex keeps ONE global memory for every project and finds entries by keyword, so similar projects
    bleed into each other. Tell it exactly which entries are this workspace's, from the bridge side."""
    mine, others = memory_scope(cdir, known)
    head = f"WORKSPACE BINDING: this run belongs to exactly one workspace: {cdir}. "
    if mine:
        rule = ("In the Memory folder, the entries that belong to this workspace are those in MEMORY.md whose "
                "`applies_to: cwd=` is: " + "; ".join(mine) + ". Use ONLY those entries (and the rollout summaries "
                "they point to). ")
    else:
        rule = ("No memory entry belongs to this workspace yet: it is a new workspace (its memory will build up "
                "from runs here). Treat memory as EMPTY: do not search MEMORY.md or rollout summaries for guidance, "
                "even if keywords look familiar. ")
    if others:
        shown = "; ".join(others[:8]) + (" ..." if len(others) > 8 else "")
        rule += ("Entries for other workspaces (" + shown + ") belong to different projects, even when names or "
                 "topics look similar: never use, quote or act on them. ")
    return head + rule + "The brief and the files in this workspace override memory."


def _codex_home():
    return os.path.expanduser(os.environ.get("CODEX_HOME", "~/.codex"))


def _quota_from_limits(rl):
    """Quota is optional telemetry: malformed fields invalidate the reading, never the run."""
    def number(value):
        if isinstance(value, bool) or not isinstance(value, (int, float, str)):
            raise ValueError("not a number")
        value = float(value)
        if not math.isfinite(value) or value < 0:
            raise ValueError("invalid number")
        return value

    try:
        if not isinstance(rl, dict):
            return None
        bits = []
        for key in ("primary", "secondary"):
            w = rl.get(key)
            if w is None:
                continue
            if not isinstance(w, dict):
                return None
            used = number(w["used_percent"])
            minutes = number(w["window_minutes"]) if w.get("window_minutes") is not None else None
            label = {10080: "weekly", 300: "5h", 1440: "daily"}.get(minutes,
                        f"{minutes:g}min" if minutes else "window")
            reset = ""
            if w.get("resets_at") is not None:
                reset = ", resets " + time.strftime("%m-%d %H:%M", time.localtime(number(w["resets_at"])))
            bits.append(f"{used:g}% of {label} used{reset}")
        credits = rl.get("credits")
        if credits is not None:
            if not isinstance(credits, dict):
                return None
            if credits.get("unlimited") is True:
                bits.append("credits unlimited")
            elif credits.get("balance") is not None:
                balance = number(credits["balance"])
                bits.append("credits " + f"{balance:,.2f}".rstrip("0").rstrip("."))
        return "; ".join(bits) or None
    except (ValueError, TypeError, KeyError, OverflowError, OSError):
        return None


QUOTA_SCAN_LIMIT = 16 * 1024 * 1024


def _last_rate_limits(path, chunk_size=262144):
    """Return (event time, limits) for the last valid reading, scanning backwards.
    Reaching the scan cap means not found, not that the session has no quota."""
    def reading(line):
        if b'"rate_limits"' not in line:
            return None
        try:
            event = json.loads(line)
            if not isinstance(event, dict):
                return None
            payload = event.get("payload")
            if not isinstance(payload, dict) or payload.get("type") != "token_count":
                return None
            limits = payload.get("rate_limits")
            if not _quota_from_limits(limits):
                return None
            stamp = event.get("timestamp")
            if not isinstance(stamp, str):
                return None
            parsed = datetime.fromisoformat(stamp.replace("Z", "+00:00"))
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            return parsed.timestamp(), limits
        except (ValueError, TypeError, OverflowError, OSError):
            return None

    try:
        with open(path, "rb") as f:
            pos = f.seek(0, os.SEEK_END)
            scanned, remainder = 0, b""
            while pos > 0 and scanned < QUOTA_SCAN_LIMIT:
                size = min(pos, chunk_size, QUOTA_SCAN_LIMIT - scanned)
                pos -= size
                f.seek(pos)
                lines = (f.read(size) + remainder).split(b"\n")
                remainder = lines.pop(0)
                for line in reversed(lines):
                    found = reading(line)
                    if found:
                        return found
                scanned += size
            if pos == 0:
                return reading(remainder)
    except (OSError, ValueError):
        pass
    return None


def _newest_quota(files):
    readings = [r for r in (_last_rate_limits(path) for path in files) if r is not None]
    return max(readings, key=lambda r: r[0]) if readings else None


def read_quota(thread):
    """Quota reported for this thread, selected by event time across matching rollouts."""
    if not thread:
        return None
    reading = _newest_quota(glob.glob(os.path.join(_codex_home(), "sessions", "*", "*", "*", f"rollout-*{thread}*.jsonl")))
    return _quota_from_limits(reading[1]) if reading else None


def cmd_usage(_):
    try:
        files = sorted(glob.glob(os.path.join(_codex_home(), "sessions", "*", "*", "*", "rollout-*.jsonl")),
                       key=os.path.getmtime, reverse=True)[:25]
        reading = _newest_quota(files)
        if reading:
            quota = _quota_from_limits(reading[1])
            if quota:
                stamp = time.strftime('%m-%d %H:%M', time.localtime(reading[0]))
                print(f"Codex quota: {quota}  (reading at {stamp})")
                return
    except Exception:
        pass  # Optional telemetry must never break a command.
    print("quota unavailable: no valid reading found in recent Codex sessions (bounded scan)")


def memory_flags(mode, native_review=False):
    flags = []
    if mode == "off" or (mode == "scoped" and native_review):
        flags += ["-c", "memories.use_memories=false"]
        if mode == "off":
            flags += ["-c", "memories.generate_memories=false"]
    return flags


INTERNAL_NOTICES = (
    ("missing tool output", re.compile(r"Custom tool call output is missing for call id")),
    ("unknown tool process", re.compile(r"codex_core::tools::router:.*write_stdin failed: Unknown process id")),
    ("model refresh timeout", re.compile(r"failed to refresh available models: request timed out")),
)


def shell_text(command):
    if os.name == "nt":
        match = re.search(r"(?i)(?:^|[\\/])(powershell|pwsh)(?:\.exe)?(?:['\"])?\s+.*?-(?:Command|c)\s+(.+)$", command)
        if match:
            text = match.group(2).strip()
            return text[1:-1] if text[:1] in ("'", '\"') and text[-1:] == text[:1] else text
    try:
        words = shlex.split(command)
        if len(words) >= 3 and os.path.basename(words[0]) in ("sh", "bash", "zsh") and words[1] in ("-c", "-lc"):
            return words[2]
    except ValueError:
        pass
    return command


def step_kind(command):
    text = shell_text(command)
    if os.name == "nt":
        if re.search(r"(?i)\bpython(?:[\d.]*|w)?(?:\.exe)?['\"]?\s+(?:-X\s+\S+\s+|-u\s+)*-m\s+(?:unittest|pytest)\b", text):
            return "test"
        if re.search(r"(?i)\b(?:Set-Content|Add-Content|Out-File|New-Item|Remove-Item|Move-Item|Copy-Item)\b", text):
            return "edit"
        if re.search(r"(?i)\b(?:Get-Content|Get-ChildItem|Get-Item)\b", text):
            return "read"
        if re.search(r"(?i)\bSelect-String\b", text):
            return "search"
    if re.search(r"\bpython[\d.]*\s+-m\s+(?:unittest|pytest)\b|(?:^|[\n;&|])\s*(?:\S*/)?pytest\b|\bunittest\.main\(|\b(npm|pnpm|yarn|cargo|go|make)\s+test\b", text):
        return "test"
    if re.search(r"apply_patch|\b(cat|tee|echo)\b[^\n]*>|\btee\s+\S+|\bsed\s+-i|\bgit\s+(add|mv|rm)\b|\.write(?:_(?:text|bytes))?\(|\bopen\([^\n]*,\s*(?:mode\s*=\s*)?[\"'][wa]|\bperl\s+-[^\s]*i", text):
        return "edit"
    if re.search(r"(?:^|[;&|]\s*)\s*(?:cat|head|tail|ls|wc)\b|\bsed\s+-n|\brg\s+--files", text):
        return "read"
    if re.search(r"\b(rg|grep|find)\b", text):
        return "search"
    if re.search(r"\bgit\b", text):
        return "git"
    return "run"


def test_result(output):
    lines = [line.strip() for line in output.splitlines() if line.strip()]
    ran = next((line for line in reversed(lines) if re.match(r"Ran \d+ tests?\b", line)), "")
    for line in reversed(lines):
        if re.match(r"(?:OK\b|FAILED\s*\()", line):
            return (ran + " " + line).strip()
        if re.search(r"\b\d+ (?:passed|failed)\b", line):
            return line.strip("= ")
    return ""


def update_steps(stats, item):
    kind = step_kind(item.get("command", ""))
    stats[kind] = stats.get(kind, 0) + 1
    if kind == "test":
        stats["test_failed"] = stats.get("test_failed", 0) + int(item.get("exit_code") not in (0, None))
        result = test_result(item.get("aggregated_output") or "")
        if result:
            stats["last_test"] = result


def steps_line(stats):
    kinds = ("read", "search", "edit", "test", "git", "run")
    parts = []
    for kind in kinds:
        if stats.get(kind):
            part = str(stats[kind]) + " " + ("other" if kind == "run" else kind)
            if kind == "test":
                part += " (" + str(stats.get("test_failed", 0)) + " failed"
                if stats.get("last_test"):
                    part += ", last: " + stats["last_test"]
                part += ")"
            parts.append(part)
    return _short("steps: " + str(sum(stats.get(k, 0) for k in kinds)) + " = " + (", ".join(parts) or "none"), 200)


def step_description(command):
    text = shell_text(command)
    heredoc = re.search(r"<<-?\s*['\"]?(\w+)['\"]?[^\n]*\n", text)
    inline = re.search(r"\b(python[\d.]*|perl|ruby|node|bash|sh)\s+-[ce]\b", text)
    if heredoc or inline:
        interpreter = re.search(r"\b(python[\d.]*|perl|ruby|node|bash|sh|cat|tee)\b", text)
        name = interpreter.group(1) if interpreter else "shell"
        body = text[heredoc.end():].splitlines() if heredoc else text.splitlines()
        if heredoc:
            body = body[:next((i for i, line in enumerate(body) if line.strip() == heredoc.group(1)), len(body))]
        # Only shell redirections on the heredoc header are evidence of a write;
        # a comparison such as `if n > 1` in the script body is not a path.
        header = text.splitlines()[0] if heredoc else ""
        path = re.search(r"(?:>{1,2}\s*|\btee\s+)([^\s<>]+)", header)
        if not path:
            path = re.search(r"Path\(['\"]([^'\"]+)['\"]\)\.write|open\(['\"]([^'\"]+)['\"]\s*,\s*['\"][wa]", text)
        target = next((value for value in path.groups() if value is not None), "") if path else ""
        return "script " + name + " (" + str(len(body)) + " lines)" + (" writes " + target.strip("'\"") if target else "")
    return " ".join(text.split())


def trace_label(command):
    description = step_description(command)
    if description.startswith("script "):
        return description.split(" writes ", 1)[-1] if " writes " in description else description
    try:
        words = win.split_args(shell_text(command)) if os.name == "nt" else shlex.split(shell_text(command))
        return next((w for w in reversed(words) if not w.startswith("-") and w not in ("cat", "ls")), "-")
    except ValueError:
        return step_description(command)


def compact_trace(jid):
    """Render execution facts, keeping failures even when they exceed the size target."""
    import difflib
    st = _jread(jid)
    rows, commands, notices = [], {}, {}
    path = Path(_job_path(jid, "events.jsonl"))
    if path.exists():
        with path.open(encoding="utf-8") as stream:
            for line in stream:
                try:
                    ev = json.loads(line)
                except ValueError:
                    ev = {"type": "raw", "text": line.rstrip()}
                item = ev.get("item") or {}
                kind = item.get("type")
                if kind == "command_execution":
                    key = item.get("id")
                    row = commands.get(key) if key is not None else None
                    if row is None:
                        row = {"type": "command", "item": {}, "number": len(commands) + 1}
                        commands[key if key is not None else object()] = row
                        rows.append(row)
                    row["item"].update(item)
                    continue
                message = event_notice(ev)
                raw = ev.get("text", "") if ev.get("type") == "raw" else ""
                if message is None and re.search(r"\b(?:warn(?:ing)?|error)\b", raw, re.IGNORECASE):
                    message = raw
                internal = next((name for name, pattern in INTERNAL_NOTICES if pattern.search(message or raw)), None)
                if internal:
                    notices[internal] = notices.get(internal, 0) + 1
                elif message:
                    rows.append({"type": "notice", "text": message[:500] + ("…" if len(message) > 500 else "")})
                elif ev.get("type") == "item.completed" and kind == "collab_tool_call":
                    tool = item.get("tool", "call").replace("_agent", "")
                    rows.append({"type": "collab", "text": "» sub-agents: " + tool + " x" + str(len(collab_receivers(ev)))})
                elif ev.get("type") == "item.completed" and kind == "agent_message":
                    text = " ".join(item.get("text", "").split())
                    text = re.split(r"(?<=[.!?])\s+|(?<=[。！？])", text, maxsplit=1)[0]
                    text = _short(text, 140)
                    if not text or re.fullmatch(r"(?i)(ok|okay|sure|understood|got it|done|ready)[.! ]*", text):
                        continue
                    if rows and rows[-1]["type"] == "narration" and difflib.SequenceMatcher(None, rows[-1]["text"], text).ratio() > .8:
                        continue
                    rows.append({"type": "narration", "text": text})
    failed = sum(row["item"].get("exit_code") not in (0, None) for row in commands.values())
    code = st.get("exit_code")
    print(f"trace job={jid} status={st.get('status', '?')} exit={code if code is not None else '-'} steps={len(commands)} failed={failed}")
    i = 0
    while i < len(rows):
        row = rows[i]
        i += 1
        if row["type"] != "command":
            print(("» " if row["type"] == "narration" else "") + row["text"])
            continue
        item = row["item"]
        kind = step_kind(item.get("command", ""))
        group = [row]
        while item.get("exit_code") == 0 and i < len(rows) and rows[i]["type"] == "command":
            other = rows[i]["item"]
            if other.get("exit_code") != 0 or step_kind(other.get("command", "")) != kind:
                break
            group.append(rows[i])
            i += 1
        description = step_description(item.get("command", ""))
        if len(group) > 1:
            if all(r["item"].get("command") == item.get("command") for r in group):
                description += " x" + str(len(group))
            else:
                labels = []
                repeats = 0
                previous = None
                for member in group:
                    command = member["item"].get("command", "")
                    if command == previous:
                        repeats += 1
                        labels[-1] = trace_label(command) + " x" + str(repeats)
                    else:
                        labels.append(trace_label(command))
                        previous, repeats = command, 1
                description = kind + "s x" + str(len(group)) + ": " + ", ".join(labels[:3])
                if len(labels) > 3:
                    description += " +" + str(len(labels) - 3)
        code = item.get("exit_code")
        number = str(row["number"]).zfill(2)
        if len(group) > 1:
            number += "-" + str(group[-1]["number"]).zfill(2)
        outputs = [str(r["item"].get("aggregated_output") or "") for r in group]
        count = sum(len(out.splitlines()) for out in outputs)
        print(number + (" ✓ " if code == 0 else " ? " if code is None else " ✗ ") + kind + " " + _short(description, 110) +
              " [exit " + str(code if code is not None else "?") + "]" + (" (+" + str(count) + " out lines)" if count else ""))
        if kind == "test":
            result = next((test_result(out) for out in reversed(outputs) if test_result(out)), "")
            if result:
                print("  " + _short(result, 200))
        if code not in (0, None):
            output = outputs[0]
            names = [line for line in output.splitlines() if re.match(r"(?:FAIL:|ERROR:|FAILED\s+\S+::)", line)]
            if kind == "test":
                for name in names[:5]:
                    print("  " + name)
            for line in [line for line in output.splitlines() if line.strip()][-3:]:
                print("  " + line)
    if notices:
        print("internal notices: " + ", ".join(name + " x" + str(count) for name, count in notices.items()))
    print("detail: " + bridge_command("log", jid, "--full"))


def event_notice(event):
    item = event.get("item") or {}
    if event.get("type") in ("error", "warning", "turn.failed"):
        message = event.get("error") or event.get("message") or event.get("text") or event
    elif isinstance(item, dict) and item.get("type") in ("error", "warning"):
        message = item.get("message") or item.get("text") or item
    elif event.get("type") == "raw" and re.search(r"\b(?:WARN(?:ING)?|ERROR)\b", event.get("text", "")):
        message = event["text"]
    else:
        return None
    return message if isinstance(message, str) else json.dumps(message, ensure_ascii=False)


def command_record(item):
    return {"command": item.get("command", ""), "exit_code": item.get("exit_code"),
            "tail": str(item.get("aggregated_output") or "").splitlines()[-3:]}


def command_line(command):
    text = str(command.get("command", "")).replace("\r", "\\r").replace("\n", "\\n")
    code = command.get("exit_code")
    return f"exit={code if code is not None else '?'} {text}"


def activity_digest(facts, state=None):
    state = state or {}
    parts = [f"{facts['commands']} commands ({facts['failed_count']} failed)"]
    files = facts.get("files")
    parts.append("files touched: " + ("unavailable" if files is None else
                 (", ".join(files[:40]) or "none") + (f" +{len(files)-40} more" if len(files) > 40 else "")))
    if facts.get("hash_capped"):
        parts.append(f"hash snapshot capped ({HASH_FILE_LIMIT} files / {HASH_BYTE_LIMIT} bytes); touched list incomplete")
    parts.append(f"error/warning events: {facts['notice_count']}")
    interrupted = state.get("status") if state.get("status") in ("cancelled", "steered") else None
    if state.get("status") in ("cancelling", "cleanup_pending"):
        interrupted = state.get("cleanup_target", "interrupted")
    parts.append("timed out: " + ("yes" if facts.get("timed_out") else "no"))
    parts.append("interrupted: " + (interrupted or facts.get("interrupted") or "no"))
    agents = facts.get("agents", {})
    if agents or facts.get("subagents") == "active":
        parts.append(f"sub-agents: {len(agents)}")
        visible = [f"{tid} model={meta.get('model') or '?'} effort={meta.get('effort') or '?'}"
                   for tid, meta in agents.items() if meta.get("model") or meta.get("effort")]
        if visible:
            parts.append("agents: " + "; ".join(visible))
    if facts.get("unattributed_collab"):
        parts.append(f"{facts['unattributed_collab']} collaboration calls without agent IDs; agent count may be incomplete")
    usage = facts.get("usage") or {}
    parts.append(f"tokens in/out {usage.get('input_tokens', '?')}/{usage.get('output_tokens', '?')}")
    parts.append("quota: " + (facts.get("quota") or "unavailable"))
    lines = [" · ".join(parts)]
    if "steps" in facts:
        lines.append(steps_line(facts["steps"]))
    for failure in facts.get("failures", []):
        lines.append("  failed command: " + command_line(failure))
        lines.extend("    " + line for line in failure["tail"])
    if facts["failed_count"] > len(facts.get("failures", [])):
        lines.append(f"  +{facts['failed_count'] - len(facts['failures'])} more failed")
    lines.extend("  error/warning: " + message for message in facts.get("notices", []))
    return "\n".join(lines)


def dispatch_prompt(cfg, settings):
    mode = cfg.get("subagents", settings["subagents"])
    if mode == "none":
        return "Do not spawn sub-agents; do all the work yourself."
    if mode == "active":
        return (
            "SUB-AGENT DISPATCH IS REQUESTED for this task: before working, split it into independent parts with disjoint "
            f"files/inputs, spawn sub-agents in parallel (up to {cfg.get('max_subagents', settings['max_subagents'])} concurrently), "
            f"choose the model per the team policy (bulk repetitive work: {settings['model_bulk']} at high; "
            f"real reasoning: {settings['model_reasoning']} at medium; otherwise the lead model {cfg['model']} at medium), "
            "give each a self-contained brief and a disjoint output location, stay responsible for integrating and "
            "spot-checking their output, and say in your final message how many sub-agents ran, with which model/effort, "
            "and what each produced. If the task cannot be split into independent parts, say so and do it yourself."
        )
    return ""  # Optional auto policy is already in eligible roles' preambles.


def collab_receivers(event):
    item = event.get("item") or {}
    if not isinstance(item, dict) or item.get("type") != "collab_tool_call":
        return set()
    ids = item.get("receiver_thread_ids") or []
    if not isinstance(ids, list):
        ids = []
    ids = ids + [item.get("receiver_thread_id")]
    states = item.get("agents_states")
    if isinstance(states, dict):
        ids += list(states)
    return {value for value in ids if isinstance(value, str) and value}


def exec_codex(cfg, prompt, cd=None, add_dir=None, session=None, resume=False, review=None,
               jid=None, live=False, kind="task", name=None):
    """Run codex once, streaming events to <run>/events.jsonl. Returns dict(ok, text, thread, error,
    usage, cfg, jid, digest). live=True also prints a readable timeline to stderr as it happens."""
    s = load()
    cfg = dict(cfg)
    for key in ("subagents", "max_subagents"):
        cfg[key] = validate(key, cfg.get(key, s[key]))
    validate_model(cfg, cd)
    if resume and cfg["sandbox"] not in SANDBOXES:
        cfg = {**cfg, "sandbox": s["sandbox"]}
    cdir = os.path.realpath(cd or os.getcwd())
    if jid is None:
        jid = new_run(kind, cfg, prompt, cdir, review, name=name)
    if review is None and prompt is not None:
        dispatch = dispatch_prompt(cfg, s)
        if dispatch:
            prompt = dispatch + "\n\n" + prompt
    elif review is not None and cfg["subagents"] != "auto":
        print(f"[codex] WARNING: subagents={cfg['subagents']} ignored for native review (no prompt)", file=sys.stderr)
    if cfg["preamble"] and prompt:
        prompt = cfg["preamble"] + "\n\n" + prompt
    memory = cfg.get("session_memory", s.get("codex_memory", "scoped"))
    _jwrite(jid, codex_memory=memory, native_review=review is not None)
    if memory == "scoped" and prompt:
        prompt = memory_guard(cdir) + "\n\n" + prompt
    cmd = cli_command(codex_bin())
    with tempfile.NamedTemporaryFile(suffix=".txt", delete=False) as output_file:
        out = output_file.name
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
    cmd += memory_flags(memory, native_review=review is not None)
    if review is not None:
        cmd += ["-c", f'sandbox_mode="{cfg["sandbox"]}"'] + review
    elif resume:
        cmd += ["-c", f'sandbox_mode="{cfg["sandbox"]}"']
    else:
        cmd += ["-s", cfg["sandbox"], "-C", cdir]
        for d in add_dir or []:
            cmd += ["--add-dir", d]
    cmd += s["extra_args"] + (["-"] if review is None else [])
    res = {"ok": False, "text": "", "thread": session, "error": "", "usage": None, "cfg": cfg,
           "jid": jid, "digest": ""}
    before = _gitstate(cdir)
    p = None
    windows_job = None
    timed_out, cleanup_errors = [], []
    msgs, errors, cmds, failed = [], [], 0, 0
    subagent_ids, subagent_meta = set(), {}
    failures, notices, notice_count = [], [], 0
    unattributed_collab = 0
    steps = {}
    raw_tail = deque(maxlen=20)
    final = ""
    execution_failed = False
    turn_completed = False

    try:
        with open(_job_path(jid, "events.jsonl"), "a", buffering=1, encoding="utf-8") as evf:
            # Publish the child group under the same lock used by cancel. A cancellation
            # before launch must prevent the child from ever being started.
            with job_state(jid) as st:
                if st.get("status") != "running":
                    raise RuntimeError("job was cancelled before launch")
                p = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                     stderr=subprocess.STDOUT, cwd=cdir, bufsize=0,
                                     **launch_options(suspended=True))
                if os.name == "nt":
                    job_name = "Local\\codex-bridge-" + hashlib.sha256((BRIDGE_HOME + jid).encode("utf-8")).hexdigest()
                    windows_job = win.contain(p, job_name)
                    st["windows_job"] = job_name
                snapshot = process_snapshot()
                identity = snapshot[p.pid][3] if snapshot is not None and p.pid in snapshot else None
                st.update(child_pid=p.pid, pgid=p.pid, child_identity=identity)
            for line in child_lines(p, prompt, cfg["timeout"], jid):
                try:
                    ev = json.loads(line)
                    if not isinstance(ev, dict):
                        raise ValueError("not an event object")
                except ValueError:
                    ev = {"type": "raw", "text": line.rstrip("\r\n")}
                    raw_tail.append(ev["text"][-1000:])
                evf.write(json.dumps(ev, ensure_ascii=False) + "\n")
                receivers = collab_receivers(ev)
                subagent_ids.update(receivers)
                item = ev.get("item") or {}
                if ev.get("type") == "item.completed" and item.get("type") == "collab_tool_call" and not receivers:
                    unattributed_collab += 1
                for tid in receivers:
                    meta = subagent_meta.setdefault(tid, {})
                    visible = (item.get("agents_states") or {}).get(tid, {}) if isinstance(item.get("agents_states"), dict) else {}
                    for key, value in (("model", item.get("model") or (visible.get("model") if isinstance(visible, dict) else None)),
                                       ("effort", item.get("effort") or item.get("reasoning_effort") or
                                        (visible.get("effort") or visible.get("reasoning_effort") if isinstance(visible, dict) else None))):
                        if isinstance(value, str):
                            meta[key] = value
                notice = event_notice(ev)
                if notice is not None:
                    notice_count += 1
                    if len(notices) < 2:
                        notices.append(notice)
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
                        cmds += 1
                        update_steps(steps, it)
                        failed += it.get("exit_code") not in (0, None)
                        if it.get("exit_code") not in (0, None) and len(failures) < 5:
                            failures.append(command_record(it))
                elif t in ("error", "turn.failed"):
                    if t == "turn.failed":
                        execution_failed = True
                    errors.append(json.dumps(ev.get("error") or ev.get("message"), ensure_ascii=False))
                elif t == "turn.completed":
                    turn_completed = True
                    res["usage"] = ev.get("usage")
                if live or notice is not None:
                    for ln in render_event(ev):
                        print(ln, file=sys.stderr, flush=True)
            p.wait(timeout=REAP_TIMEOUT)
            with Path(out).open(newline="", encoding="utf-8") as stream:
                final = stream.read()
    except subprocess.TimeoutExpired:
        timed_out.append(True)
    except Exception as exc:
        execution_failed = True
        errors.append(f"codex execution failed: {exc}")
    finally:
        if p:
            try:
                if timed_out or execution_failed or not turn_completed or not (final or (msgs[-1] if msgs else "")) or p.poll() != 0:
                    terminate_group(p.pid, process=p, jid=jid)
            except (OSError, subprocess.SubprocessError) as exc:
                cleanup_errors.append(str(exc))
            try:
                p.wait(timeout=REAP_TIMEOUT)
            except subprocess.TimeoutExpired:
                cleanup_errors.append("codex did not exit before the final wait deadline")
            for pipe in (p.stdin, p.stdout):
                if pipe:
                    pipe.close()
        if windows_job is not None:
            win.close(windows_job)
        Path(out).unlink(missing_ok=True)
    res["exit_code"] = p.returncode if p else None
    res["text"] = final or (msgs[-1] if msgs else "")
    res["ok"] = bool(p and p.returncode == 0 and res["text"] and turn_completed and not timed_out and not execution_failed and not cleanup_errors)
    res["warnings"] = errors if not execution_failed else []
    res["cleanup_pending"] = bool(cleanup_errors)
    res["cleanup_error"] = "; ".join(cleanup_errors)
    res["timed_out"] = bool(timed_out)
    if not res["ok"]:
        if not turn_completed:
            errors.append("no turn.completed event received")
        res["error"] = (f"codex timed out after {cfg['timeout']}s" if timed_out
                        else "\n".join(errors) or f"exit {p.returncode if p else 'not started'}")
        if raw_tail:
            res["error"] += "\nCLI output tail:\n" + "\n".join(raw_tail)[-4000:]
        if cleanup_errors:
            res["error"] += "\nprocess cleanup failed: " + "; ".join(cleanup_errors)
    after = _gitstate(cdir, before["hashes"] if before else ())
    changed = (sorted(name for name, digest in after["hashes"].items()
                      if before["hashes"].get(name) != digest)
               if before is not None and after is not None else None)
    subagent_ids.discard(res["thread"])
    res["subagent_count"] = len(subagent_ids)
    try:
        res["quota"] = read_quota(res["thread"])
    except Exception:
        res["quota"] = None  # Includes parallel tasks: telemetry never changes their results.
    state = _jread(jid) or {}
    facts = {"commands": cmds, "failed_count": failed, "failures": failures, "files": changed,
             "hash_capped": bool((before and before["capped"]) or (after and after["capped"])),
             "notice_count": notice_count, "notices": notices, "timed_out": bool(timed_out),
             "interrupted": state.get("cleanup_target") if state.get("status") in ACTIVE else None,
             "agents": {tid: subagent_meta.get(tid, {}) for tid in sorted(subagent_ids)},
             "subagents": cfg["subagents"], "unattributed_collab": unattributed_collab,
             "usage": res["usage"], "quota": res["quota"], "steps": steps}
    res["digest"] = activity_digest(facts, state)
    _jwrite(jid, digest=res["digest"], activity=facts, thread=res["thread"], quota=res["quota"], exit_code=res["exit_code"],
            subagent_count=res["subagent_count"])
    return res


def _short(t, n=160):
    t = " ".join(str(t).split())
    return t if len(t) <= n else t[:n - 1] + "…"


def render_event(ev, full=False):
    """Turn one codex JSONL event into human-readable lines ([] = skip)."""
    t = ev.get("type"); it = ev.get("item", {}) or {}; ty = it.get("type")
    if t == "raw":
        return [ev.get("text", "") if full else _short(ev.get("text", ""), 400)]
    if t == "turn.started":
        return ["── turn started ──"]
    if t == "turn.completed":
        u = ev.get("usage", {})
        return [f"── turn done · tokens in/out {u.get('input_tokens','?')}/{u.get('output_tokens','?')} ──"]
    if t in ("error", "warning", "turn.failed"):
        return ["✘ " + _short(json.dumps(ev.get("error") or ev.get("message"), ensure_ascii=False), 300)]
    if t == "item.started" and ty == "collab_tool_call":
        return []
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
        if ty == "collab_tool_call":
            n = len(it.get("receiver_thread_ids") or [])
            return [f"🧩 sub-agents: {it.get('tool')} ({n} agent{'s' if n != 1 else ''}) {it.get('status','')}"]
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


def bridge_command(*args):
    if os.name == "nt":
        command = powershell_command([sys.executable, "-X", "utf8", os.path.join(BRIDGE_HOME, "bin", "codex_bridge.py"), *args])
        if BRIDGE_HOME != os.path.realpath(os.path.expanduser("~/.claude/codex-bridge")):
            command = "$env:CODEX_BRIDGE_HOME='" + BRIDGE_HOME.replace("'", "''") + "'; " + command
        return command
    command = shlex.join([os.path.join(BRIDGE_HOME, "bin", "codex_bridge.py"), *args])
    default_home = os.path.realpath(os.path.expanduser("~/.claude/codex-bridge"))
    if BRIDGE_HOME != default_home:
        command = f"CODEX_BRIDGE_HOME={shlex.quote(BRIDGE_HOME)} " + command
    return command


def print_footer(r):
    jid = r["jid"]
    print("more: " + " | ".join(bridge_command(*args) for args in (
        ("log", jid), ("log", jid, "--failed"), ("log", jid, "--grep", "P"), ("attach", jid))))


def saved_report(jid):
    path = Path(_job_path(jid, "result.txt"))
    if path.exists():
        with path.open(newline="", encoding="utf-8") as stream:
            return stream.read()
    return ""


def print_report(jid):
    st = _jread(jid) or {}
    status = st.get("status", "failed")
    if status == "failed" and st.get("timed_out"):
        status = "timeout"
    code = st.get("exit_code")
    print(f"codex {status} exit={code if code is not None else '-'} job={jid} session={st.get('thread') or '-'}")
    print("report:")
    text = saved_report(jid)
    sys.stdout.write(text)
    if text and not text.endswith("\n"):
        print()
    print("activity: " + (activity_digest(st["activity"], st) if st.get("activity") else st.get("digest", "")))
    if status in ("failed", "timeout", "cleanup_pending"):
        print("error: " + st.get("error", "")[-1200:])
    print_footer({"jid": jid})


def report_run(r):
    print_report(r["jid"])
    if not r["ok"]:
        sys.exit(1)


def find_session(cwd=None, thread=None, name=None):
    """Look up a Codex session recorded by this bridge: newest task/resume session in `cwd`,
    or the one with id `thread`. Returns the job state (has thread/model/effort) or None."""
    cwd = os.path.realpath(cwd) if cwd else None
    if not os.path.isdir(JOBS):
        return None
    for jid in ordered_jobs():
        st = _jread(jid) or {}
        if not st.get("thread"):
            continue
        if thread and st["thread"] == thread:
            return st
        if name and st.get("name") == name and os.path.realpath(st.get("cwd", "")) == cwd:
            return st
        if not thread and not name and os.path.realpath(st.get("cwd", "")) == cwd and st.get("kind") in ("task", "resume"):
            return st
    return None


def prepare_run(a, resume):
    cfg = resolve(a, load())
    session = getattr(a, "session", None)
    label = getattr(a, "name", None)
    cwd_ = os.path.realpath(a.cd or os.getcwd())
    if label:
        for jid in os.listdir(JOBS) if os.path.isdir(JOBS) else []:
            st = _refresh(jid)
            if st.get("name") == label and os.path.realpath(st.get("cwd", "")) == cwd_ and st.get("status") in ACTIVE:
                sys.exit(f"session {label!r} already has active job {jid}; cancel or wait for it first")
    st = (find_session(thread=session) if session else
          find_session(cwd=cwd_, name=label) if label else
          find_session(cwd=cwd_) if resume or getattr(a, "cont", False) else None)
    if st:
        session, resume = st["thread"], True
        label = label or st.get("name")
        # Every entry point uses the same metadata and locks the original pair.
        for k in ("model", "effort", "sandbox"):
            asked = cfg[k]
            inherited = st.get(k, "unknown" if k == "sandbox" else cfg[k])
            if asked != inherited:
                print(f"[codex] ignoring requested {k}={asked}: session {session} inherits "
                      f"{k}={inherited}. To change it, start a NEW session with `run`.", file=sys.stderr)
            cfg[k] = inherited
        cfg["profile"] = st.get("profile", cfg["profile"])
        cfg["session_memory"] = st.get("codex_memory", load().get("codex_memory", "scoped"))
    elif session:
        resume = True
        cfg["sandbox"] = "unknown"
        print(f"[codex] WARNING: session {session} is not recorded by this bridge; model/effort "
              "cannot be verified. Keep the original pair; the configured sandbox default will apply.", file=sys.stderr)
    elif label:
        resume = False  # a new named session must never fall through to native --last
    elif resume:
        sys.exit("no earlier Codex session from this bridge in this directory; start with `run`")
    elif getattr(a, "cont", False):
        print("[codex] --continue: no earlier session here, starting a new one", file=sys.stderr)
    if session and cfg["sandbox"] not in SANDBOXES:
        cfg["sandbox"] = load()["sandbox"]
        print(f"[codex] WARNING: sandbox was not recorded; applying configured default {cfg['sandbox']}", file=sys.stderr)
    if session:
        for jid in os.listdir(JOBS) if os.path.isdir(JOBS) else []:
            active = _refresh(jid)
            if active.get("thread") == session and active.get("status") in ACTIVE:
                sys.exit(f"session {session} already has active job {jid}; cancel or wait for it first")
    prompt = a.prompt
    if prompt in (None, "-"):
        prompt = sys.stdin.read()
    if not prompt.strip():
        sys.exit("empty prompt")
    validate_model(cfg, cwd_)
    jid = new_run("resume" if resume else "task", cfg, prompt, cwd_, name=label)
    if session:
        _jwrite(jid, thread=session)
    return cfg, prompt, session, resume, label, jid


def run_codex(a, resume):
    if a.prompt in (None, "-"):
        a.prompt = sys.stdin.read()
    label = getattr(a, "name", None)
    identity = json.dumps([os.path.realpath(a.cd or os.getcwd()), label])
    lock = os.path.join(BRIDGE_HOME, "sessions", hashlib.sha256(identity.encode()).hexdigest() + ".lock")
    with file_lock(lock) if label else contextlib.nullcontext():
        with file_lock(os.path.join(BRIDGE_HOME, "sessions", "index.lock")):
            cfg, prompt, session, resume, label, jid = prepare_run(a, resume)
    if a.background:
        return start_job("resume" if resume else "task", cfg, prompt, a.cd, a.add_dir, resume, session, name=label, jid=jid)
    r = exec_codex(cfg, prompt, a.cd, a.add_dir, session, resume, live=getattr(a, "live", False),
                   kind="resume" if resume else "task", name=label, jid=jid)
    finish_run(r)
    report_run(r)


JOBS = os.path.join(BRIDGE_HOME, "jobs")
ADVERSARIAL = ("You are performing an ADVERSARIAL review. Your job is to break confidence in the change, not to "
               "validate it: assume it can fail in subtle, costly ways. Hunt for auth/trust-boundary gaps, data loss, "
               "rollback/retry/idempotency holes, races, empty/null/timeout behaviour, version/schema drift, and "
               "observability gaps. Challenge the approach and design choices too, not only defects. Report only "
               "material findings, ranked by severity, each with file:line, a concrete failure scenario and a fix. "
               "No style nits. If nothing material survives, say so. Do not modify any files.\n\n")


def _job_path(jid, name):
    return os.path.join(JOBS, jid, name)


@contextlib.contextmanager
def job_state(jid):
    path = _job_path(jid, "state.json")
    with file_lock(_job_path(jid, "state.lock")):
        st = _jread(jid) or {}
        yield st
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(st, f, ensure_ascii=False)
        replace_file(tmp, path)


def _jwrite(jid, **kw):
    with job_state(jid) as st:
        st.update(kw)


def _jread(jid):
    if jid is None:
        return None
    try:
        return json.loads(read_owned(_job_path(jid, "state.json")))
    except PermissionError:
        if os.name == "nt":
            raise  # a busy state file must never be mistaken for absent state
        return None
    except (OSError, ValueError):
        return None


ACTIVE = {"running", "cancelling", "cleanup_pending"}
CANCEL_GRACE = 1.0


PS_TIMEOUT = 0.5
REAP_TIMEOUT = 1.0
PIPE_DRAIN_TIMEOUT = 1.0


def process_snapshot():
    """None means enumeration unavailable, not an empty process table."""
    if os.name == "nt":
        return win.snapshot()
    try:
        result = subprocess.run(["ps", "-A", "-o", "pid=,ppid=,pgid=,stat=,lstart="],
                                capture_output=True, text=True, timeout=PS_TIMEOUT,
                                env={**os.environ, "LC_ALL": "C"}, encoding="utf-8")
        if result.returncode:
            return None
        rows = {}
        for line in result.stdout.splitlines():
            pid, parent, group, status, started = line.split(None, 4)
            rows[int(pid)] = (int(parent), int(group), status, " ".join(started.split()))
        return rows or None
    except (OSError, ValueError, subprocess.SubprocessError):
        return None


def _alive(pid):
    if not isinstance(pid, int) or pid <= 0:
        return False
    if os.name == "nt":
        return win.alive(pid)
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # lack of permission is not proof of exit


def group_alive(pgid, snapshot=None):
    if not isinstance(pgid, int) or pgid <= 1:
        return False
    if os.name == "nt":
        return _alive(pgid)
    try:
        os.killpg(pgid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    if snapshot is None:
        snapshot = process_snapshot()
    if snapshot is None:
        return True
    return any(group == pgid and not status.startswith("Z") for _, group, status, _ in snapshot.values())


def recorded_processes(st):
    # Legacy PID-only lists are intentionally not trusted for signalling.
    return {int(pid): dict(record) for pid, record in st.get("tracked_processes", {}).items()}


def verify_processes(tracked, snapshot):
    if snapshot is None:
        return dict(tracked)  # retain uncertainty for a later scan, never signal these blindly
    return {pid: {"identity": record["identity"], "pgid": snapshot[pid][1]}
            for pid, record in tracked.items()
            if pid in snapshot and snapshot[pid][3] == record["identity"]
            and not snapshot[pid][2].startswith("Z")}


def collect_descendants(root, tracked, snapshot, root_identity=None, owned_child=False, excluded_group=None):
    """Only a verified process identity can establish ownership of descendants."""
    tracked = verify_processes(tracked, snapshot)
    if snapshot is None:
        return tracked
    row = snapshot.get(root)
    if row and not row[2].startswith("Z") and (row[3] == root_identity or (not root_identity and owned_child)):
        tracked[root] = {"identity": row[3], "pgid": row[1]}
    while True:
        previous = len(tracked)
        groups = set() if os.name == "nt" else {r["pgid"] for r in tracked.values() if r["pgid"] > 1 and r["pgid"] != os.getpgrp() and r["pgid"] != excluded_group}
        for pid, (parent, group, status, identity) in snapshot.items():
            if pid != os.getpid() and pid != root and (parent in tracked or group in groups) and not status.startswith("Z"):
                tracked[pid] = {"identity": identity, "pgid": group}
        if len(tracked) == previous:
            break
    tracked.pop(os.getpid(), None)
    return tracked


def remember_processes(jid, tracked, snapshot):
    if jid is None:
        return
    with job_state(jid) as st:
        merged = recorded_processes(st)
        merged.update(tracked)
        merged = verify_processes(merged, snapshot)
        st["tracked_processes"] = {str(pid): record for pid, record in merged.items()}
        st["tracked_pids"] = sorted(merged)
        st["tracked_pgids"] = sorted({record["pgid"] for record in merged.values()})
        root = st.get("child_pid")
        if not st.get("child_identity") and root in tracked:
            st["child_identity"] = tracked[root]["identity"]
        if snapshot is not None and root in snapshot and st.get("child_identity"):
            if snapshot[root][3] != st["child_identity"]:
                st["child_identity_mismatch"] = True
        if snapshot is None:
            st["process_scan_warning"] = "ps unavailable; signalling falls back to the original child group only"


def targets_alive(tracked, snapshot, fallback_group=None):
    if snapshot is not None:
        return bool(verify_processes(tracked, snapshot))
    # An unverified PID may have been reused, so this is only an uncertainty
    # check. It must not authorize signalling or a terminal transition.
    if any(_alive(pid) for pid in tracked):
        return True
    if fallback_group:
        if os.name == "nt":
            return _alive(fallback_group)
        try:
            os.killpg(fallback_group, 0)
            return True
        except ProcessLookupError:
            pass
        except PermissionError:
            return True
    return False


def terminate_group(pgid, process=None, jid=None, legacy_root=None):
    """Bounded tree cleanup; verify identities before discovery and signalling."""
    if os.name == "nt":
        return terminate_windows(pgid, process, jid, legacy_root)
    if legacy_root is None and (not isinstance(pgid, int) or pgid <= 1 or pgid == os.getpgrp()):
        raise OSError(f"unsafe or missing process group: {pgid}")
    st = (_jread(jid) or {}) if jid else {}
    tracked = recorded_processes(st)
    root = legacy_root[0] if legacy_root else pgid
    excluded_group = legacy_root[2] if legacy_root else None
    root_identity = legacy_root[1] if legacy_root else st.get("child_identity")
    root_reused = st.get("child_identity_mismatch", False)
    failures = []
    for sig in (signal.SIGTERM, signal.SIGKILL):
        deadline = time.monotonic() + CANCEL_GRACE
        signalled_pids, signalled_groups = set(), set()
        while True:
            owned_child = process is not None and process.poll() is None
            snapshot = process_snapshot()
            if jid and not root_identity:
                root_identity = (_jread(jid) or {}).get("child_identity")
            if snapshot is not None and root in snapshot and root_identity:
                root_reused = root_reused or snapshot[root][3] != root_identity
            tracked = collect_descendants(root, tracked, snapshot, root_identity, owned_child and not root_reused, excluded_group)
            if not root_identity and root in tracked:
                root_identity = tracked[root]["identity"]
            remember_processes(jid, tracked, snapshot)
            # Re-check immediately before sending signals. Group ownership needs
            # a currently verified member; a recycled numeric PGID is not enough.
            current = process_snapshot()
            tracked = verify_processes(tracked, current)
            if current is not None and root in current and root_identity:
                root_reused = root_reused or current[root][3] != root_identity
            remember_processes(jid, tracked, current)
            fallback = pgid if not root_reused and legacy_root is None else None
            groups = ({record["pgid"] for record in tracked.values()} if current is not None
                      else ({fallback} if fallback else set()))
            groups.discard(os.getpgrp())
            groups.discard(excluded_group)
            pids = set(tracked) if current is not None else set()
            for group in sorted(groups, key=lambda g: g == pgid):
                # Anchor the signal to member identities, not just the group ID.
                anchor = tuple(sorted((pid, r["identity"]) for pid, r in tracked.items() if r["pgid"] == group))
                token = (group, anchor)
                if group <= 1 or token in signalled_groups:
                    continue
                try:
                    os.killpg(group, sig)
                except ProcessLookupError:
                    pass
                except OSError as exc:
                    failures.append(str(exc))
                signalled_groups.add(token)
            for pid in sorted(pids, key=lambda pid: pid == pgid):
                token = (pid, tracked[pid]["identity"])
                if pid <= 1 or pid == os.getpid() or token in signalled_pids:
                    continue
                try:
                    os.kill(pid, sig)
                except ProcessLookupError:
                    pass
                except OSError as exc:
                    failures.append(str(exc))
                signalled_pids.add(token)
            if process:
                process.poll()
            snapshot = process_snapshot()
            tracked = verify_processes(tracked, snapshot)
            remember_processes(jid, tracked, snapshot)
            unknown_root = (snapshot is not None and root in snapshot and not root_identity
                            and not snapshot[root][2].startswith("Z") and not owned_child)
            if not unknown_root and not targets_alive(tracked, snapshot, fallback):
                return
            if time.monotonic() >= deadline:
                break
            time.sleep(0.05)
    raise OSError("process tree still alive or exit unconfirmed after SIGKILL" +
                  (": " + "; ".join(failures[-3:]) if failures else ""))


def terminate_windows(pgid, process=None, jid=None, legacy_root=None):
    root = legacy_root[0] if legacy_root else pgid
    if not isinstance(root, int) or root <= 1 or root == os.getpid():
        raise OSError(f"unsafe or missing process id: {root}")
    state = (_jread(jid) or {}) if jid else {}
    expected = legacy_root[1] if legacy_root else state.get("child_identity")
    tracked = recorded_processes(state)
    deadline = time.monotonic() + 5.0
    suspended = {}
    job_name = state.get("windows_job")
    if job_name:
        # A named Job Object retains kernel ownership even if the root exits or
        # Toolhelp/creation-time queries fail. No numeric PID authorizes this kill.
        win.kill_job(job_name)
        while win.job_members(job_name):
            if time.monotonic() >= deadline:
                raise OSError("Windows job cleanup exceeded deadline")
            if process is not None:
                process.poll()
            time.sleep(0.02)
        remember_processes(jid, {}, process_snapshot())
        return
    try:
        while True:
            rows = process_snapshot()
            if rows is None:
                raise OSError("Windows process enumeration unavailable; cleanup unconfirmed")
            row = rows.get(root)
            if row and expected and row[3] != expected:
                if jid:
                    _jwrite(jid, child_identity_mismatch=True)
                # Previously verified descendants are still ours; the reused root is not.
                tracked.pop(root, None)
            tracked = collect_descendants(root, tracked, rows, expected,
                                           process is not None and process.poll() is None and not expected)
            if not expected and root in tracked:
                expected = tracked[root]["identity"]
            remember_processes(jid, tracked, rows)
            if not tracked:
                if row and not expected:
                    raise OSError("process identity unavailable; refusing unverified PID")
                return
            # Stop verified parents before discovering/killing children, so they cannot
            # replace descendants during cleanup. Validation and action share a HANDLE.
            for pid in sorted(tracked, key=lambda pid: pid != root):
                identity = tracked[pid]["identity"]
                if pid not in suspended and win.signal_verified(pid, identity, stop=True):
                    suspended[pid] = identity
            current = process_snapshot()
            if current is None:
                raise OSError("cannot verify suspended Windows process tree")
            expanded = collect_descendants(root, tracked, current, expected)
            if set(expanded) != set(tracked):
                tracked = expanded
                if time.monotonic() >= deadline:
                    raise OSError("Windows process cleanup exceeded deadline")
                continue
            tracked = expanded
            remember_processes(jid, tracked, current)
            for pid in sorted(tracked, key=lambda pid: pid == root):
                win.signal_verified(pid, tracked[pid]["identity"])
            if process is not None:
                process.poll()
            if time.monotonic() >= deadline:
                raise OSError("Windows process cleanup exceeded deadline")
            time.sleep(0.02)
    finally:
        # If access failed, do not leave surviving owned processes suspended.
        for pid, identity in suspended.items():
            win.resume_verified(pid, identity)


def windows_child_lines(process, prompt, timeout, jid):
    events = queue.Queue(maxsize=64)
    stopped = threading.Event()

    def publish(value):
        while not stopped.is_set():
            try:
                events.put(value, timeout=0.05)
                return
            except queue.Full:
                pass

    def read_output():
        try:
            while not stopped.is_set():
                chunk = process.stdout.read(65536)
                if not chunk:
                    break
                publish(chunk)
        except (OSError, ValueError) as exc:
            publish(exc)
        finally:
            publish(None)

    def write_input():
        try:
            pending = memoryview((prompt or "").encode("utf-8"))
            while pending and not stopped.is_set():
                count = process.stdin.write(pending[:65536])
                if not count:
                    break
                pending = pending[count:]
        except (BrokenPipeError, OSError, ValueError):
            pass
        finally:
            try:
                process.stdin.close()
            except OSError:
                pass

    reader = threading.Thread(target=read_output, daemon=True)
    writer = threading.Thread(target=write_input, daemon=True)
    reader.start()
    writer.start()
    deadline = time.monotonic() + timeout
    drain = cancel = None
    next_scan = 0
    tracked = {}
    identity = (_jread(jid) or {}).get("child_identity")
    buffer = b""
    eof = False
    try:
        while not eof or process.poll() is None:
            now = time.monotonic()
            if now >= deadline:
                raise subprocess.TimeoutExpired("codex", timeout)
            if now >= next_scan:
                rows = process_snapshot()
                if rows is not None:
                    for pid in win.job_members((_jread(jid) or {}).get("windows_job")):
                        if pid in rows:
                            tracked[pid] = {"identity": rows[pid][3], "pgid": 0}
                tracked = collect_descendants(process.pid, tracked, rows, identity, process.poll() is None)
                if not identity and process.pid in tracked:
                    identity = tracked[process.pid]["identity"]
                remember_processes(jid, tracked, rows)
                next_scan = time.monotonic() + 0.5
            if (_jread(jid) or {}).get("status") in ("cancelling", "cancelled", "steered"):
                cancel = cancel or now + 2 * CANCEL_GRACE + 2 * PS_TIMEOUT + REAP_TIMEOUT
                if now >= cancel:
                    raise RuntimeError("cancelled job exceeded its pipe/exit deadline")
            if process.poll() is not None and not eof:
                drain = drain or now + PIPE_DRAIN_TIMEOUT
                if now >= drain:
                    raise RuntimeError("codex exited but a descendant kept stdout open")
            try:
                chunk = events.get(timeout=min(0.05, max(0, deadline - now)))
            except queue.Empty:
                continue
            if isinstance(chunk, Exception):
                raise chunk
            if chunk is None:
                eof = True
                if buffer:
                    yield buffer.decode("utf-8", errors="replace")
                    buffer = b""
                continue
            buffer += chunk
            while b"\n" in buffer:
                line, buffer = buffer.split(b"\n", 1)
                yield line.decode("utf-8", errors="replace")
    finally:
        stopped.set()
        # Cancel pending synchronous reads/writes before the caller closes pipes;
        # otherwise CloseHandle can wait for a live child even after the deadline.
        for thread in (reader, writer):
            if thread.is_alive():
                win.cancel_io(thread.native_id)
        for thread in (reader, writer):
            thread.join(timeout=0.2)


def child_lines(process, prompt, timeout, jid):
    """Nonblocking pipes: neither partial stdout nor unread stdin can defeat a deadline."""
    if os.name == "nt":
        yield from windows_child_lines(process, prompt, timeout, jid)
        return
    deadline = time.monotonic() + timeout
    drain_deadline = cancel_deadline = None
    next_scan = 0
    pending = memoryview((prompt or "").encode())
    buffer = b""
    tracked = {}
    root_identity = (_jread(jid) or {}).get("child_identity")
    with selectors.DefaultSelector() as selector:
        for pipe in (process.stdin, process.stdout):
            os.set_blocking(pipe.fileno(), False)
        selector.register(process.stdout, selectors.EVENT_READ)
        if pending:
            selector.register(process.stdin, selectors.EVENT_WRITE)
        else:
            process.stdin.close()
        while selector.get_map() or process.poll() is None:
            now = time.monotonic()
            if now >= deadline:
                raise subprocess.TimeoutExpired("codex", timeout)
            if now >= next_scan:
                snapshot = process_snapshot()
                tracked = collect_descendants(process.pid, tracked, snapshot, root_identity, process.poll() is None)
                if not root_identity and process.pid in tracked:
                    root_identity = tracked[process.pid]["identity"]
                remember_processes(jid, tracked, snapshot)
                next_scan = time.monotonic() + 0.5
            if (_jread(jid) or {}).get("status") in ("cancelling", "cancelled", "steered"):
                if cancel_deadline is None:
                    cancel_deadline = now + 2 * CANCEL_GRACE + 2 * PS_TIMEOUT + REAP_TIMEOUT
                if now >= cancel_deadline:
                    raise RuntimeError("cancelled job exceeded its pipe/exit deadline")
            if process.poll() is not None:
                drain_deadline = drain_deadline or now + PIPE_DRAIN_TIMEOUT
                if now >= drain_deadline:
                    raise RuntimeError("codex exited but a descendant kept stdout open")
            for key, mask in selector.select(timeout=min(0.1, max(0, deadline - now))):
                if key.fileobj is process.stdin:
                    try:
                        pending = pending[os.write(process.stdin.fileno(), pending[:65536]):]
                    except BrokenPipeError:
                        pending = pending[len(pending):]
                    except BlockingIOError:
                        continue
                    if not pending:
                        selector.unregister(process.stdin)
                        process.stdin.close()
                else:
                    try:
                        chunk = os.read(process.stdout.fileno(), 65536)
                    except BlockingIOError:
                        continue
                    buffer += chunk
                    while b"\n" in buffer:
                        line, buffer = buffer.split(b"\n", 1)
                        yield line.decode("utf-8", errors="replace")
                    if not chunk:
                        selector.unregister(process.stdout)
                        if buffer:
                            yield buffer.decode("utf-8", errors="replace")
                            buffer = b""


def new_run(kind, cfg, prompt, cd, review=None, spec=None, name=None):
    import time, uuid
    jid = time.strftime("%m%d-%H%M%S-") + uuid.uuid4().hex[:4]
    os.makedirs(os.path.join(JOBS, jid))
    if spec:
        Path(_job_path(jid, "spec.json")).write_text(json.dumps(spec, ensure_ascii=False), encoding="utf-8")
    _jwrite(jid, id=jid, kind=kind, status="running", cwd=os.path.realpath(cd), model=cfg["model"], effort=cfg["effort"],
            started=time.time(), pid=os.getpid(), pgid=None, name=name, sandbox=cfg["sandbox"], profile=cfg["profile"],
            codex_memory=cfg.get("session_memory", load().get("codex_memory", "scoped")), native_review=review is not None,
            exit_code=None,
            subagents=cfg.get("subagents", load()["subagents"]), max_subagents=cfg.get("max_subagents", load()["max_subagents"]),
            title=(prompt or " ".join(review or []))[:70].replace("\n", " "))
    return jid


def finish_run(r):
    with job_state(r["jid"]) as st:
        st["worker_finished"] = True
        st.update(exit_code=r.get("exit_code"))
        if st.get("status") in ("cancelled", "steered"):
            return
        if r.get("cleanup_pending"):
            st.update(status="cleanup_pending",
                      cleanup_target=st.get("cleanup_target", "failed"),
                      cleanup_error=r["cleanup_error"], error=r["error"],
                      timed_out=r.get("timed_out", False), usage=r["usage"])
            st.pop("finished", None)
            write_report(_job_path(r["jid"], "result.txt"), r.get("text", ""))
            return
        if st.get("status") in ("cancelling", "cleanup_pending"):
            return
        write_report(_job_path(r["jid"], "result.txt"), r["text"])
        st.update(status="done" if r["ok"] else "failed", usage=r["usage"],
                  error=r["error"], timed_out=r.get("timed_out", False), finished=time.time())


def start_job(kind, cfg, prompt, cd, add_dir=None, resume=False, session=None, review=None, name=None, jid=None):
    cd = os.path.realpath(cd or os.getcwd())
    if jid is None:
        jid = new_run(kind, cfg, prompt, cd, review, name=name)
    atomic_json(_job_path(jid, "spec.json"),
                {"cfg": cfg, "prompt": prompt, "cd": cd, "add_dir": add_dir,
                 "resume": resume, "session": session, "review": review})
    try:
        with job_state(jid) as st:
            if st.get("status") != "running":
                print(f"job {jid} is {st.get('status')}; worker was not launched")
                return
            with open(_job_path(jid, "log.txt"), "w", encoding="utf-8") as log:
                pr = subprocess.Popen([sys.executable, "-X", "utf8", os.path.realpath(__file__), "_job", jid],
                                      stdout=log, stderr=log, stdin=subprocess.DEVNULL, **launch_options(),
                                      env={**os.environ, "CODEX_BRIDGE_HOME": BRIDGE_HOME})
            st["pid"] = pr.pid
            if os.name == "nt":
                rows = process_snapshot()
                st["worker_identity"] = rows[pr.pid][3] if rows is not None and pr.pid in rows else None
    except OSError as exc:
        finish_run({"jid": jid, "ok": False, "text": "", "error": f"cannot launch worker: {exc}", "usage": None})
        sys.exit(f"cannot launch worker: {exc}")
    print(f"started background job {jid} ({kind}, {cfg['model']} {cfg['effort']}).")
    for label, command in (("watch live", "watch"), ("transcript", "log"), ("result", "result"),
                           ("wait", "wait"), ("cancel", "cancel"), ("take over", "attach")):
        print(f"  {label:<10}: {bridge_command(command, jid)}")
    print("  reminder  : open the 'watch live' command in a terminal pane tab now so the user can see Codex work "
          "(desktop app: mcp__terminal__run_in_terminal, load it with ToolSearch if it is deferred).")



def cmd__job(a):
    jid = a.jid
    try:
        spec = json.loads(read_owned(_job_path(jid, "spec.json")))
        r = exec_codex(spec["cfg"], spec["prompt"], spec["cd"], spec["add_dir"], spec["session"],
                       spec["resume"], spec["review"], jid=jid)
    except (Exception, SystemExit) as exc:
        r = {"jid": jid, "ok": False, "text": "", "error": str(exc), "usage": None}
    finish_run(r)


def ordered_jobs():
    states = [(jid, _jread(jid)) for jid in os.listdir(JOBS)] if os.path.isdir(JOBS) else []
    return [jid for jid, st in sorted(((jid, st) for jid, st in states if st),
                                     key=lambda pair: pair[1].get("started", 0), reverse=True)]


def scoped_jobs(name=None):
    ids = ordered_jobs()
    cwd = os.path.realpath(os.getcwd())
    local = [jid for jid in ids if (st := _jread(jid))
             and os.path.realpath(st.get("cwd", "")) == cwd
             and (not name or st.get("name") == name)]
    if local or name:
        return local
    if ids:
        print(f"[codex] no jobs in {cwd}; falling back to global newest", file=sys.stderr)
    return ids


def _resolve_job(ref=None, name=None):
    if name:
        if ref not in (None, "last"):
            sys.exit("choose either a job id or --name")
        ids = scoped_jobs(name)
        if not ids:
            sys.exit(f"no job named {name!r} in the current directory")
        return ids[0]
    if ref in (None, "last"):
        ids = scoped_jobs()
        if not ids:
            sys.exit("no jobs yet")
        return ids[0]
    ids = os.listdir(JOBS) if os.path.isdir(JOBS) else []
    matches = [i for i in ids if i == ref or i.endswith(ref)]
    if len(matches) != 1:
        sys.exit(f"job '{ref}' not found or ambiguous")
    return matches[0]


def _refresh(jid):
    with job_state(jid) as st:
        if st.get("status") in ACTIVE:
            snapshot = process_snapshot()
            worker_alive = (st.get("pid") in snapshot and not snapshot[st["pid"]][2].startswith("Z")) if snapshot is not None else _alive(st.get("pid"))
            if os.name == "nt" and worker_alive and st.get("worker_identity"):
                worker_alive = win.alive(st["pid"], st["worker_identity"])
            worker_alive = worker_alive and not st.get("worker_finished")
            tracked = recorded_processes(st)
            root, identity = st.get("child_pid"), st.get("child_identity")
            if root and identity and not st.get("child_identity_mismatch"):
                tracked.setdefault(root, {"identity": identity, "pgid": st.get("pgid", root)})
            tracked = verify_processes(tracked, snapshot)
            st["tracked_processes"] = {str(pid): record for pid, record in tracked.items()}
            st["tracked_pids"] = sorted(tracked)
            st["tracked_pgids"] = sorted({record["pgid"] for record in tracked.values()})
            if snapshot is not None and root in snapshot and identity and snapshot[root][3] != identity:
                st["child_identity_mismatch"] = True
            fallback = None if st.get("child_identity_mismatch") else st.get("pgid")
            execution_alive = targets_alive(tracked, snapshot, fallback)
            if root and not identity and _alive(root):
                execution_alive = True  # legacy/initially unobservable child: cannot confirm exit
            if not worker_alive and not execution_alive:
                if st["status"] in ("cancelling", "cleanup_pending"):
                    target = st.get("cleanup_target", "cancelled" if st["status"] == "cancelling" else "failed")
                    if target == "steered" and not st.get("steered_to"):
                        if _alive(st.get("steering_pid")):
                            return dict(st)  # steering still owns the replacement reservation
                        target = "failed"
                        st["error"] = "steering interrupted before replacement job was reserved"
                    st.update(status=target, finished=time.time())
                    if target == "failed":
                        Path(_job_path(jid, "result.txt")).write_text("FAILED: " + st.get("error", "cleanup completed"), encoding="utf-8")
                else:
                    st.update(status="failed", error="worker process gone (legacy job)" if "pgid" not in st else "worker process died")
        return dict(st)


def compact_state(st):
    state = {k: st.get(k) for k in ("id", "status", "name", "thread", "model", "effort", "sandbox", "digest", "error", "steered_to", "steered_from", "exit_code", "subagents", "subagent_count")}

    if st.get("activity"):
        state["digest"] = activity_digest(st["activity"], st)
    facts = st.get("activity") or {}
    state["counts"] = {key: facts.get(key) for key in ("commands", "failed_count", "notice_count")}
    if state.get("error"):
        state["error"] = state["error"][-1200:]
    return state


def cmd_jobs(a):
    import time
    ids = scoped_jobs(getattr(a, "name", None))[:a.limit]
    if getattr(a, "json", False):
        print(json.dumps([compact_state(_refresh(jid)) for jid in ids], separators=(",", ":")))
        return
    if not ids:
        print("no jobs"); return
    print(f"{'ID':<17}{'KIND':<8}{'STATUS':<10}{'AGE':<7}{'MODEL/EFFORT':<22}TASK")
    for jid in ids:
        st = _refresh(jid)
        age = f"{int((time.time()-st.get('started', time.time()))/60)}m"
        print(f"{jid:<17}{st.get('kind',''):<8}{st.get('status',''):<10}{age:<7}"
              f"{st.get('model','')+' '+st.get('effort',''):<22}{st.get('title','')}"
              + (f" steered -> {st['steered_to']}" if st.get("steered_to") else "")
              + (f" steered from {st['steered_from']}" if st.get("steered_from") else ""))


def cmd_result(a):
    jid = _resolve_job(a.job, getattr(a, "name", None)); st = _refresh(jid)
    if st["status"] in ACTIVE:
        print(f"job {jid} still running"); return
    if getattr(a, "full", False):
        sys.stdout.write(saved_report(jid))
    else:
        print_report(jid)


def cmd_wait(a):
    import time
    jid = _resolve_job(a.job, getattr(a, "name", None)); t0 = time.time()
    while _refresh(jid)["status"] in ACTIVE:
        if time.time() - t0 > a.timeout:
            print(f"job {jid} still running after {a.timeout}s"); return
        time.sleep(3)
    cmd_result(argparse.Namespace(job=jid, full=getattr(a, "full", False)))


def legacy_process(jid, st):
    """Verify the legacy root without trusting a recycled numeric PID."""
    pid = st.get("pid")
    manual = f"Stop-Process -Id {pid} -Force" if os.name == "nt" else f"kill {pid}"
    before = process_snapshot()
    row = before.get(pid) if before is not None else None
    if (before is not None and (row is None or row[2].startswith("Z"))) or not _alive(pid):
        with job_state(jid) as state:
            state.update(status="failed", error="worker process gone (legacy job)", finished=time.time())
        print(f"worker process gone (legacy job); cannot verify pid {pid}; manual command: {manual}", file=sys.stderr)
        return None
    try:
        if os.name == "nt":
            argv = win.split_args(win.command_line(pid))
        else:
            result = subprocess.run(["ps", "-p", str(pid), "-o", "command="], capture_output=True,
                                    text=True, timeout=PS_TIMEOUT, check=True, encoding="utf-8")
            argv = shlex.split(result.stdout.strip())
        worker = (any(os.path.basename(v) == "codex_bridge.py" for v in argv)
                  and any(argv[i:i+2] == ["_job", jid] for i in range(len(argv))))
        # A recorded direct Codex child is also eligible; never accept arbitrary
        # shell text merely mentioning Codex or the job id.
        executable = os.path.basename(argv[0]) if argv else ""
        codex = (pid == st.get("child_pid") and executable in ("codex", "codex.exe") and "exec" in argv)
        after = process_snapshot()
        recorded_identity = st.get("child_identity") if pid == st.get("child_pid") else st.get("worker_identity")
        if (row and after is not None and after.get(pid) == row and (worker or codex)
                and (not recorded_identity or row[3] == recorded_identity)):
            return pid, row[3], row[1]
    except (OSError, ValueError, subprocess.SubprocessError):
        pass
    with job_state(jid) as state:
        if state.get("status") == "cleanup_pending" and not state.get("pgid"):
            state.update(status="running", error="legacy process identity unverified")
    sys.exit(f"cannot verify legacy pid {pid} as codex_bridge.py _job {jid} or recorded Codex child; "
             f"nothing signalled; manual command: {manual}")


def stop_job(jid, target="cancelled", defer_terminal=False):
    st = _jread(jid) or {}
    if st.get("status") not in ACTIVE:
        return False
    legacy = ("pgid" not in st or (st.get("pgid") is None and st.get("thread") and st.get("pid") != os.getpid()))
    root = legacy_process(jid, st) if legacy else None
    if legacy and root is None:
        return False
    with job_state(jid) as st:
        if st.get("status") not in ACTIVE:
            return False
        st.update(status="cancelling", cleanup_target=target)
        if target == "steered":
            st["steering_pid"] = os.getpid()
        st["cancel_reason"] = "interrupted for user guidance" if target == "steered" else "cancel requested"
        pgid = st.get("pgid")
    try:
        if legacy:
            terminate_group(None, jid=jid, legacy_root=root)
        elif pgid is not None:
            terminate_group(pgid, jid=jid)
    except OSError as exc:
        with job_state(jid) as st:
            st.update(status="cleanup_pending", cleanup_target="failed" if target == "steered" else target, cleanup_error=str(exc),
                      error=(st.get("error", "") + f"\ncancellation failed: {exc}").strip())
        sys.exit(f"cancellation failed for {jid}: {exc}; status remains cleanup_pending")
    if not defer_terminal:
        with job_state(jid) as st:
            st.update(status=target, finished=time.time())
    return True


def cmd_cancel(a):
    jid = _resolve_job(a.job, getattr(a, "name", None))
    stopped = stop_job(jid)
    print(f"cancelled {jid}" if stopped else f"job {jid} is {(_jread(jid) or {}).get('status')}")


STEER_SESSION_WAIT = 20.0
STEER_FRAME = (
    "The user was watching your work live and sends this guidance. "
    "Your previous turn was interrupted: any command that was running was stopped and may have partially run, "
    "so check the current state of files/git before redoing anything. Do not repeat finished steps. "
    "Continue the original task with the guidance applied, and start your reply with one line saying how your plan changed."
)


def cmd_steer(a):
    message = sys.stdin.read() if a.message == "-" else a.message
    if not message.strip():
        sys.exit("empty guidance message")
    jid = _resolve_job(a.job, getattr(a, "name", None))
    deadline = time.monotonic() + STEER_SESSION_WAIT
    while True:
        old = _jread(jid) or {}
        if old.get("thread"):
            break
        if time.monotonic() >= deadline:
            sys.exit("session not started yet, retry shortly; nothing interrupted")
        time.sleep(0.1)
    prompt = STEER_FRAME + "\n\n" + message
    # All run/resume entry points reserve sessions under this lock. Hold it
    # across cleanup and reservation so no competing continuation slips in.
    with file_lock(os.path.join(BRIDGE_HOME, "sessions", "index.lock")):
        old = _jread(jid)
        for other in ordered_jobs():
            if other == jid:
                continue
            st = _refresh(other)
            same_name = old.get("name") and st.get("name") == old["name"] and st.get("cwd") == old["cwd"]
            if st.get("status") in ACTIVE and (st.get("thread") == old["thread"] or same_name):
                sys.exit(f"session {old['thread']} already has active job {other}; nothing interrupted")
        settings = load()
        cfg = resolve(a, settings)
        for key in ("model", "effort", "sandbox", "profile", "codex_memory"):
            cfg["session_memory" if key == "codex_memory" else key] = old.get(key, settings.get(key))
        if cfg["sandbox"] not in SANDBOXES:
            cfg["sandbox"] = settings["sandbox"]
        validate_model(cfg, old["cwd"])
        interrupted = stop_job(jid, "steered", defer_terminal=True)
        new = new_run("resume", cfg, prompt, old["cwd"], name=old.get("name"))
        _jwrite(new, thread=old["thread"], steered_from=jid)
        with job_state(jid) as state:
            state["steered_to"] = new
            if interrupted:
                state.update(status="steered", finished=time.time())
    if not a.foreground:
        return start_job("resume", cfg, prompt, old["cwd"], resume=True, session=old["thread"],
                         name=old.get("name"), jid=new)
    r = exec_codex(cfg, prompt, old["cwd"], session=old["thread"], resume=True, jid=new, live=getattr(a, "live", False))
    finish_run(r)
    report_run(r)

def _stream(jid, follow, full=False, since=0, tail=None):
    if since < 0 or (tail is not None and tail < 0):
        sys.exit("--since and --tail must be nonnegative")
    path = _job_path(jid, "events.jsonl")
    if follow:
        while not os.path.exists(path) and _refresh(jid).get("status") in ACTIVE:
            time.sleep(0.2)
    if not os.path.exists(path):
        st = _refresh(jid)
        if follow and st.get("steered_to"):
            print(f"steered -> {st['steered_to']}")
        print("next cursor: 0")
        return
    st = _jread(jid) or {}
    if follow:
        print(f"═ codex run {jid} · {st.get('model')} {st.get('effort')} · {st.get('cwd')}", flush=True)
    cursor = 0
    with open(path, encoding="utf-8") as f:
        if tail is not None:
            lines = deque(maxlen=tail)
            for index, line in enumerate(f, 1):
                if line.endswith("\n"):
                    cursor = index
                    if index > since:
                        lines.append(line)
            for line in lines:
                for rendered in render_event(json.loads(line), full):
                    print(rendered)
        else:
            while True:
                offset = f.tell()
                line = f.readline()
                if line and line.endswith("\n"):
                    cursor += 1
                    if cursor > since:
                        try:
                            for rendered in render_event(json.loads(line), full):
                                print(rendered, flush=True)
                        except ValueError:
                            pass
                    continue
                f.seek(offset)  # do not consume an event still being written
                if not follow or _refresh(jid).get("status") not in ACTIVE:
                    break
                time.sleep(0.5)
    if follow:
        st = _refresh(jid)
        link = f" -> {st['steered_to']}" if st.get("steered_to") else ""
        print(f"═ {st.get('status')}{link} · {st.get('digest', '')}")
    print(f"next cursor: {cursor}", flush=True)


def cmd_watch(a):
    _stream(_resolve_job(a.job, getattr(a, "name", None)), follow=True)


def targeted_log(jid, a):
    pattern = None
    if getattr(a, "grep", None) is not None:
        try:
            pattern = re.compile(a.grep)
        except re.error as exc:
            sys.exit(f"invalid --grep pattern: {exc}")
    since, tail = getattr(a, "since", None) or 0, getattr(a, "tail", None)
    if since < 0 or (tail is not None and tail < 0):
        sys.exit("--since and --tail must be nonnegative")
    events, cursor = [], 0
    path = Path(_job_path(jid, "events.jsonl"))
    if path.exists():
        with path.open(encoding="utf-8") as source:
            for cursor, line in enumerate(source, 1):
                if cursor <= since or not line.endswith("\n"):
                    continue
                try:
                    event = json.loads(line)
                    if isinstance(event, dict):
                        events.append(event)
                except ValueError:
                    pass
    if tail is not None:
        events = events[-tail:] if tail else []
    if pattern is not None:
        lines = [line for event in events for rendered in render_event(event, full=True) for line in rendered.splitlines()]
        selected = set()
        for i, line in enumerate(lines):
            if pattern.search(line):
                selected.update(range(max(0, i - 2), min(len(lines), i + 3)))
        previous = -1
        for i in sorted(selected):
            if previous >= 0 and i > previous + 1:
                print("--")
            print(lines[i])
            previous = i
    else:
        commands = {}
        for i, event in enumerate(events):
            item = event.get("item") or {}
            if isinstance(item, dict) and item.get("type") == "command_execution":
                commands[item.get("id") or str(i)] = command_record(item)
        for command in commands.values():
            if getattr(a, "failed", False) and command["exit_code"] in (None, 0):
                continue
            print(command_line(command))
            if getattr(a, "failed", False):
                for line in command["tail"]:
                    print("  " + line)
    print(f"next cursor: {cursor}")


def cmd_log(a):
    jid = _resolve_job(a.job, getattr(a, "name", None))
    if getattr(a, "failed", False) or getattr(a, "commands", False) or getattr(a, "grep", None) is not None:
        return targeted_log(jid, a)
    if a.full or getattr(a, "since", None) is not None or getattr(a, "tail", None) is not None:
        return _stream(jid, follow=False, full=a.full, since=getattr(a, "since", None) or 0, tail=getattr(a, "tail", None))
    compact_trace(jid)


def cmd_attach(a):
    jid = _resolve_job(a.job, getattr(a, "name", None)); st = _refresh(jid)
    if st.get("status") in ACTIVE and not getattr(a, "force", False):
        sys.exit(f"job {jid} is still {st['status']}; cancel/wait first, or use attach --force")
    if not st.get("thread"):
        sys.exit("no codex session id recorded yet for this run")
    cmd = cli_command(codex_bin())
    if st.get("profile"):
        cmd += ["-p", st["profile"]]
    cmd += ["resume", st["thread"]]
    if st.get("model"):
        cmd += ["-m", st["model"]]
    if st.get("effort"):
        cmd += ["-c", f'model_reasoning_effort="{st["effort"]}"']
    sandbox = st.get("sandbox")
    if sandbox not in SANDBOXES:
        sandbox = load()["sandbox"]
    cmd += ["-c", f'sandbox_mode="{sandbox}"', "-c", 'approval_policy="never"']
    memory = st.get("codex_memory")
    if memory is None:
        memory = load().get("codex_memory", "scoped")
    cmd += memory_flags(memory, native_review=st.get("native_review", False))
    if os.name == "nt":
        print("Set-Location -LiteralPath '" + st["cwd"].replace("'", "''") + "'; " + powershell_command(cmd))
    else:
        print(f"cd {shlex.quote(st['cwd'])} && {shlex.join(cmd)}")
    print("# Run the line above in a terminal to continue this exact Codex session interactively (you drive).", file=sys.stderr)


def cmd_review(a):
    a.role = "reviewer"  # review_model/review_effort from settings unless --model/--effort given
    cfg = resolve(a, load())
    cfg["sandbox"] = "read-only"
    validate_model(cfg, a.cd)
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
    if a.background:
        return start_job("review", cfg, prompt, a.cd, review=review)
    r = exec_codex(cfg, prompt, a.cd, review=review, live=getattr(a, "live", False), kind="review")
    finish_run(r)
    report_run(r)

def cmd_parallel(a):
    """Run many tasks concurrently. Input: JSON list (file or '-'):
    [{"name":"a","role":"explorer","prompt":"...","cd":"/path","model":..,"effort":..,"sandbox":..}]
    Tasks writing to the same files must not run in parallel."""
    from concurrent.futures import ThreadPoolExecutor
    raw = sys.stdin.read() if a.file == "-" else Path(a.file).read_text(encoding="utf-8")
    tasks = json.loads(raw)
    s = load()

    if not isinstance(tasks, list) or not tasks:
        sys.exit("parallel expects a nonempty JSON task list")
    configs = []
    for task in tasks:
        ns = argparse.Namespace(**{k: task.get(k) for k in ("role", "model", "effort", "sandbox", "profile", "timeout", "subagents", "max_subagents")})
        cfg = resolve(ns, s)
        validate_model(cfg, task.get("cd"))
        configs.append(cfg)
    workers = validate("max_parallel", a.max if a.max is not None else s["max_parallel"])

    def one(i_t):
        i, t = i_t
        cfg = configs[i]
        r = exec_codex(cfg, t["prompt"], t.get("cd"), t.get("add_dir"), kind="task")
        finish_run(r)
        return t.get("name") or f"task{i+1}", t.get("role"), r

    with ThreadPoolExecutor(max_workers=min(workers, len(tasks))) as ex:
        results = list(ex.map(one, enumerate(tasks)))
    failed = 0
    for name, role, r in results:
        print_report(r["jid"])
        failed += not r["ok"]
    sys.exit(1 if failed else 0)


def cmd_roles(_):
    roles = {**ROLES, **load().get("roles", {})}
    for n, r in roles.items():
        print(f"{n:<10} sandbox={r.get('sandbox','-'):<18} effort={r.get('effort','(follows config)'):<17} "
              f"model={r.get('model','(default)')}  {r.get('preamble','')[:70]}")


def refresh_bridge_link():
    target = os.path.realpath(__file__)
    directory = os.path.join(BRIDGE_HOME, "bin")
    os.makedirs(directory, exist_ok=True)
    if os.name == "nt":
        launcher = os.path.join(directory, "codex_bridge.py")
        # os.execv on Windows spawns a new process and exits the caller with 0, losing stdin, ordering and the exit code.
        content = "import runpy\nrunpy.run_path(" + repr(target) + ", run_name='__main__')\n"
        with file_lock(launcher + ".lock"):
            if os.path.exists(launcher) and read_owned(launcher) == content:
                return
            fd, temporary = tempfile.mkstemp(dir=directory, prefix=".launcher-")
            try:
                with os.fdopen(fd, "w", encoding="utf-8", newline="") as stream:
                    stream.write(content)
                replace_file(temporary, launcher)
            finally:
                Path(temporary).unlink(missing_ok=True)
        return
    fd, tmp = tempfile.mkstemp(dir=directory, prefix=".link-")
    os.close(fd)
    os.unlink(tmp)
    try:
        os.symlink(target, tmp)
        replace_file(tmp, os.path.join(directory, "codex_bridge.py"))
    finally:
        Path(tmp).unlink(missing_ok=True)


def main():
    refresh_bridge_link()
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name in ("run", "resume"):
        r = sub.add_parser(name)
        r.add_argument("prompt", nargs="?")
        r.add_argument("--model"); r.add_argument("--effort"); r.add_argument("--sandbox")
        r.add_argument("--role", help="explorer|worker|debugger|reviewer|architect|custom")
        r.add_argument("--cd"); r.add_argument("--add-dir", action="append")
        r.add_argument("--timeout", type=int); r.add_argument("--profile")
        r.add_argument("--live", action="store_true", help="print human timeline to stderr (default: final report only)")
        r.add_argument("--subagents", help="none|auto|active; prompt-level dispatch policy")
        r.add_argument("--max-subagents", type=int, help="maximum concurrent sub-agents, 1..20")
        r.add_argument("--background", action="store_true", help="return a job id immediately")
        r.add_argument("--continue", dest="cont", action="store_true", help="continue the latest session in this directory (same context/model/effort)")
        r.add_argument("--name", help="named session: reuse the session labelled NAME here if it exists (inherits its model/effort), else start a new one and label it")
        if name == "resume":
            r.add_argument("--session", help="session id; default: most recent")
    c = sub.add_parser("config")
    c.add_argument("action", nargs="?", default="show", choices=["show", "set", "reset"])
    c.add_argument("pairs", nargs="*")
    sub.add_parser("models"); sub.add_parser("roles"); sub.add_parser("usage")
    status = sub.add_parser("status")
    status.add_argument("job", nargs="?", default="last"); status.add_argument("--name")
    status.add_argument("--json", action="store_true")
    rv = sub.add_parser("review"); rv.add_argument("--live", action="store_true"); rv.add_argument("focus", nargs="*")
    g = rv.add_mutually_exclusive_group(); g.add_argument("--base"); g.add_argument("--commit")
    rv.add_argument("--adversarial", action="store_true"); rv.add_argument("--background", action="store_true")
    for x in ("model", "effort", "cd", "timeout", "profile"):
        rv.add_argument("--" + x, type=int if x == "timeout" else None)
    jb = sub.add_parser("jobs"); jb.add_argument("--limit", type=int, default=15); jb.add_argument("--name"); jb.add_argument("--json", action="store_true")
    for n in ("watch", "log", "attach"):
        q = sub.add_parser(n); q.add_argument("job", nargs="?", default="last"); q.add_argument("--name")
        if n == "log":
            q.add_argument("--full", action="store_true", help="detailed transcript instead of the compact execution trace")
            filters = q.add_mutually_exclusive_group()
            filters.add_argument("--failed", action="store_true", help="failed commands with exit codes and last 3 output lines")
            filters.add_argument("--commands", action="store_true", help="every command, one line each, with exit code")
            filters.add_argument("--grep", metavar="PATTERN", help="regex over rendered lines, with 2 lines of context")
            q.add_argument("--since", type=int, default=None); q.add_argument("--tail", type=int)
        if n == "attach": q.add_argument("--force", action="store_true")
    for n in ("result", "cancel", "wait"):
        q = sub.add_parser(n); q.add_argument("job", nargs="?", default="last"); q.add_argument("--name")
        if n in ("result", "wait"):
            q.add_argument("--full", action="store_true", help="print only the verbatim saved report")
        if n == "wait": q.add_argument("--timeout", type=int, default=600)
    steer = sub.add_parser("steer", help="interrupt safely and guide the same session (background by default)")
    steer.add_argument("job", nargs="?", default="last")
    steer.add_argument("--name", help="select a named session in this directory")
    steer.add_argument("--live", action="store_true", help="print human timeline to stderr in foreground mode")
    steer.add_argument("--subagents", help="none|auto|active; per-turn dispatch policy")
    steer.add_argument("--max-subagents", type=int, help="maximum concurrent sub-agents, 1..20")
    steer.add_argument("--foreground", action="store_true")
    steer.add_argument("message", help="guidance text, or - to read stdin")
    sub.add_parser("_job").add_argument("jid")
    pl = sub.add_parser("parallel"); pl.add_argument("file", help="JSON task list file or -")
    pl.add_argument("--max", type=int)
    a = ap.parse_args()
    {"config": cmd_config, "models": cmd_models, "status": cmd_status, "roles": cmd_roles, "usage": cmd_usage, "parallel": cmd_parallel,
        "review": cmd_review, "watch": cmd_watch, "log": cmd_log, "attach": cmd_attach, "jobs": cmd_jobs, "result": cmd_result, "wait": cmd_wait, "cancel": cmd_cancel, "steer": cmd_steer, "_job": cmd__job}.get(
        a.cmd, lambda x: run_codex(x, a.cmd == "resume"))(a)


if __name__ == "__main__":
    main()
