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
import argparse, glob, json, os, shutil, subprocess, sys, tempfile
import contextlib, fcntl, re, signal, time, hashlib, shlex, selectors, math
from datetime import datetime, timezone
from pathlib import Path
from collections import deque

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
    with open(path, "a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        yield


def atomic_json(path, value):
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path), prefix=".bridge-")
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(value, f, indent=2, ensure_ascii=False)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    finally:
        Path(tmp).unlink(missing_ok=True)


def _load_settings():
    try:
        with open(SETTINGS) as f:
            stored = json.load(f)
        if not isinstance(stored, dict):
            raise ValueError("expected a JSON object")
    except FileNotFoundError:
        if os.path.lexists(SETTINGS):
            sys.exit(f"cannot read settings {SETTINGS}: broken symlink")
        stored = {}
    except (OSError, ValueError) as exc:
        sys.exit(f"cannot read settings {SETTINGS}: {exc}")
    try:
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

    if k not in DEFAULTS:
        fail(f"unknown key; keys: {', '.join(DEFAULTS)}")
    if k in ("effort", "review_effort"):
        if not isinstance(v, str) or v not in EFFORTS:
            fail(f"must be one of {sorted(EFFORTS)}")
    elif k == "sandbox":
        if not isinstance(v, str) or v not in SANDBOXES:
            fail(f"must be one of {sorted(SANDBOXES)}")
    elif k in ("timeout", "max_parallel"):
        try:
            if isinstance(v, bool) or not isinstance(v, (int, str)):
                raise ValueError()
            value = int(v)
            if value < 1:
                raise ValueError()
            return value
        except (ValueError, TypeError):
            fail("must be a positive integer")
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
                v = json.loads(v) if v.lstrip().startswith("[") else shlex.split(v)
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
    with file_lock(SETTINGS + ".lock"):
        if a.action == "reset":
            backup = None
            try:
                _load_settings()
            except SystemExit:
                backup = SETTINGS + ".corrupt-" + str(time.time_ns())
                try:
                    os.replace(SETTINGS, backup)
                except OSError as exc:
                    sys.exit(f"cannot back up corrupt settings {SETTINGS}: {exc}")
            s = dict(DEFAULTS)
            try:
                atomic_json(SETTINGS, s)
            except OSError:
                if backup:
                    os.replace(backup, SETTINGS)
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
    command = [binary] + (["-p", profile] if profile else []) + ["debug", "models"]
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
            cached = json.loads(Path(path).read_text())
            if cached.get("binary") == binary and cached.get("context") == context and 0 <= time.time() - cached["fetched"] < 3600:
                return cached["models"]
        except (OSError, ValueError, KeyError, TypeError):
            pass
        try:
            out = subprocess.run(command, cwd=context["cwd"], capture_output=True, text=True, timeout=30)
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
        text = native.read_text()
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
    if getattr(a, "json", False):
        print(json.dumps(compact_state(_refresh(_resolve_job(a.job, a.name))), separators=(",", ":")))
        return
    print(subprocess.run([codex_bin(), "--version"], capture_output=True, text=True).stdout.strip())
    print("settings file:", SETTINGS)
    print(json.dumps(load(), indent=2, ensure_ascii=False))


def team_policy(s):
    return (
        "\n\nTEAM POLICY (you are the lead; sub-agents are OPTIONAL, use them only when work splits into independent "
        "parts with DISJOINT files/inputs; never for small tasks). Pick the model per sub-agent when you spawn it:\n"
        f"- Highly repetitive, simple per-item work (bulk edits, per-file/per-record transforms, format conversion, "
        f"extraction, mechanical checks): fan out `{s['model_bulk']}` sub-agents, up to 20 running concurrently "
        f"(it handles high concurrency well), reasoning effort high (xhigh only if items are subtle). Use it when "
        f"there are roughly 10+ independent items: one sub-agent per item or per small shard, disjoint outputs; for "
        f"large batches use many shards (up to the 20 concurrent) to cut latency; for fewer items just do the work yourself. Spot-check a sample of their outputs before reporting.\n"
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
    cfg = {k: g(k) for k in ("model", "effort", "sandbox", "timeout", "profile")}
    if rname in ("explorer", "reviewer", "architect"):
        cfg["sandbox"] = getattr(a, "sandbox", None) or "read-only"
    validate("effort", cfg["effort"]); validate("sandbox", cfg["sandbox"])
    cfg["timeout"] = validate("timeout", cfg["timeout"])
    cfg["preamble"] = role.get("preamble", "")
    if s.get("team_policy") and rname in ("worker", "debugger", "architect"):
        cfg["preamble"] += team_policy(s)
    return cfg


HASH_FILE_LIMIT = 200
HASH_BYTE_LIMIT = 32 * 1024 * 1024


def _gitstate(cd, include=()):
    try:
        root = subprocess.run(["git", "rev-parse", "--show-toplevel"], cwd=cd,
                              capture_output=True, text=True, timeout=20)
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
                    for item in re.split(r",\s*(?=/)", m.group(1)):
                        item = item.strip()
                        if not item.startswith("/") or "{" in item or "}" in item or "\x00" in item:  # skip brace patterns / junk
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
                             capture_output=True, text=True, timeout=3, check=True)
        root = os.path.realpath(top.stdout.strip())
        common = subprocess.run(["git", "rev-parse", "--git-common-dir"], cwd=cdir,
                                capture_output=True, text=True, timeout=3, check=True)
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


def exec_codex(cfg, prompt, cd=None, add_dir=None, session=None, resume=False, review=None,
               jid=None, live=False, kind="task", name=None):
    """Run codex once, streaming events to <run>/events.jsonl. Returns dict(ok, text, thread, error,
    usage, cfg, jid, digest). live=True also prints a readable timeline to stderr as it happens."""
    s = load()
    validate_model(cfg, cd)
    if resume and cfg["sandbox"] not in SANDBOXES:
        cfg = {**cfg, "sandbox": s["sandbox"]}
    cdir = os.path.realpath(cd or os.getcwd())
    if jid is None:
        jid = new_run(kind, cfg, prompt, cdir, review, name=name)
    if cfg["preamble"] and prompt:
        prompt = cfg["preamble"] + "\n\n" + prompt
    memory = s.get("codex_memory", "scoped")
    _jwrite(jid, codex_memory=memory, native_review=review is not None)
    if memory == "scoped" and prompt:
        prompt = memory_guard(cdir) + "\n\n" + prompt
    cmd = [codex_bin()]
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
    timed_out, cleanup_errors = [], []
    msgs, errors, cmds, failed = [], [], 0, 0
    raw_tail = deque(maxlen=20)
    final = ""
    execution_failed = False
    turn_completed = False

    try:
        with open(_job_path(jid, "events.jsonl"), "a", buffering=1) as evf:
            # Publish the child group under the same lock used by cancel. A cancellation
            # before launch must prevent the child from ever being started.
            with job_state(jid) as st:
                if st.get("status") != "running":
                    raise RuntimeError("job was cancelled before launch")
                p = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                     stderr=subprocess.STDOUT, cwd=cdir, bufsize=0,
                                     start_new_session=True)
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
                        failed += it.get("exit_code") not in (0, None)
                elif t in ("error", "turn.failed"):
                    if t == "turn.failed":
                        execution_failed = True
                    errors.append(json.dumps(ev.get("error") or ev.get("message"), ensure_ascii=False))
                elif t == "turn.completed":
                    turn_completed = True
                    res["usage"] = ev.get("usage")
                if live:
                    for ln in render_event(ev):
                        print(ln, file=sys.stderr, flush=True)
            p.wait(timeout=REAP_TIMEOUT)
            final = Path(out).read_text().strip()
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
        Path(out).unlink(missing_ok=True)
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
    u = res["usage"] or {}
    parts = [f"{cmds} commands" + (f" ({failed} failed)" if failed else "")]
    if changed is not None:
        parts.append("files touched: " + (", ".join(changed[:15]) + (" ..." if len(changed) > 15 else "") if changed else "none"))
    if (before and before["capped"]) or (after and after["capped"]):
        parts.append(f"hash snapshot capped ({HASH_FILE_LIMIT} files / {HASH_BYTE_LIMIT} bytes); touched list incomplete")
    parts.append(f"tokens in/out {u.get('input_tokens', '?')}/{u.get('output_tokens', '?')}")
    try:
        res["quota"] = read_quota(res["thread"])
    except Exception:
        res["quota"] = None  # Includes parallel tasks: telemetry never changes their results.
    if res["quota"]:
        parts.append("quota: " + res["quota"])
    res["digest"] = " · ".join(parts)
    _jwrite(jid, digest=res["digest"], thread=res["thread"], quota=res["quota"])
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
    if t in ("error", "turn.failed"):
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
    command = shlex.join([os.path.join(BRIDGE_HOME, "bin", "codex_bridge.py"), *args])
    default_home = os.path.realpath(os.path.expanduser("~/.claude/codex-bridge"))
    if BRIDGE_HOME != default_home:
        command = f"CODEX_BRIDGE_HOME={shlex.quote(BRIDGE_HOME)} " + command
    return command


def print_footer(r):
    print(f"\n--- Codex activity: {r['digest']}\n    run {r['jid']} · session {r['thread']}"
          f"\n    transcript: {bridge_command('log', r['jid'])}"
          f"\n    take over : {bridge_command('attach', r['jid'])}")


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
    print(header(cfg, getattr(a, "role", None)) + (" (session inherited)" if resume else ""), file=sys.stderr)
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
    r = exec_codex(cfg, prompt, a.cd, a.add_dir, session, resume, live=True,
                   kind="resume" if resume else "task", name=label, jid=jid)
    finish_run(r)
    if not r["ok"]:
        sys.exit(f"CODEX FAILED: {r['error']}\n(run {r['jid']}; transcript: {bridge_command('log', r['jid'])})")
    print(r["text"])
    print_footer(r)


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
    with open(_job_path(jid, "state.lock"), "a") as lk:
        fcntl.flock(lk, fcntl.LOCK_EX)
        st = _jread(jid) or {}
        yield st
        tmp = path + ".tmp"
        with open(tmp, "w") as f:
            json.dump(st, f, ensure_ascii=False)
        os.replace(tmp, path)


def _jwrite(jid, **kw):
    with job_state(jid) as st:
        st.update(kw)


def _jread(jid):
    try:
        with open(_job_path(jid, "state.json")) as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


ACTIVE = {"running", "cancelling", "cleanup_pending"}
CANCEL_GRACE = 1.0


PS_TIMEOUT = 0.5
REAP_TIMEOUT = 1.0
PIPE_DRAIN_TIMEOUT = 1.0


def process_snapshot():
    """None means enumeration unavailable, not an empty process table."""
    try:
        result = subprocess.run(["ps", "-A", "-o", "pid=,ppid=,pgid=,stat=,lstart="],
                                capture_output=True, text=True, timeout=PS_TIMEOUT,
                                env={**os.environ, "LC_ALL": "C"})
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


def collect_descendants(root, tracked, snapshot, root_identity=None, owned_child=False):
    """Only a verified process identity can establish ownership of descendants."""
    tracked = verify_processes(tracked, snapshot)
    if snapshot is None:
        return tracked
    row = snapshot.get(root)
    if row and not row[2].startswith("Z") and (row[3] == root_identity or (not root_identity and owned_child)):
        tracked[root] = {"identity": row[3], "pgid": row[1]}
    while True:
        previous = len(tracked)
        groups = {r["pgid"] for r in tracked.values() if r["pgid"] > 1 and r["pgid"] != os.getpgrp()}
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
        try:
            os.killpg(fallback_group, 0)
            return True
        except ProcessLookupError:
            pass
        except PermissionError:
            return True
    return False


def terminate_group(pgid, process=None, jid=None):
    """Bounded tree cleanup; verify identities before discovery and signalling."""
    if not isinstance(pgid, int) or pgid <= 1 or pgid == os.getpgrp():
        raise OSError(f"unsafe or missing process group: {pgid}")
    st = (_jread(jid) or {}) if jid else {}
    tracked = recorded_processes(st)
    root_identity = st.get("child_identity")
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
            if snapshot is not None and pgid in snapshot and root_identity:
                root_reused = root_reused or snapshot[pgid][3] != root_identity
            tracked = collect_descendants(pgid, tracked, snapshot, root_identity, owned_child and not root_reused)
            if not root_identity and pgid in tracked:
                root_identity = tracked[pgid]["identity"]
            remember_processes(jid, tracked, snapshot)
            # Re-check immediately before sending signals. Group ownership needs
            # a currently verified member; a recycled numeric PGID is not enough.
            current = process_snapshot()
            tracked = verify_processes(tracked, current)
            if current is not None and pgid in current and root_identity:
                root_reused = root_reused or current[pgid][3] != root_identity
            remember_processes(jid, tracked, current)
            fallback = pgid if not root_reused else None
            groups = ({record["pgid"] for record in tracked.values()} if current is not None
                      else ({fallback} if fallback else set()))
            groups.discard(os.getpgrp())
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
            unknown_root = (snapshot is not None and pgid in snapshot and not root_identity
                            and not snapshot[pgid][2].startswith("Z") and not owned_child)
            if not unknown_root and not targets_alive(tracked, snapshot, fallback):
                return
            if time.monotonic() >= deadline:
                break
            time.sleep(0.05)
    raise OSError("process tree still alive or exit unconfirmed after SIGKILL" +
                  (": " + "; ".join(failures[-3:]) if failures else ""))


def child_lines(process, prompt, timeout, jid):
    """Nonblocking pipes: neither partial stdout nor unread stdin can defeat a deadline."""
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
            if (_jread(jid) or {}).get("status") in ("cancelling", "cancelled"):
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
        Path(_job_path(jid, "spec.json")).write_text(json.dumps(spec, ensure_ascii=False))
    _jwrite(jid, id=jid, kind=kind, status="running", cwd=os.path.realpath(cd), model=cfg["model"], effort=cfg["effort"],
            started=time.time(), pid=os.getpid(), pgid=None, name=name, sandbox=cfg["sandbox"], profile=cfg["profile"],
            codex_memory=load().get("codex_memory", "scoped"), native_review=review is not None,
            title=(prompt or " ".join(review or []))[:70].replace("\n", " "))
    return jid


def finish_run(r):
    with job_state(r["jid"]) as st:
        st["worker_finished"] = True
        if st.get("status") == "cancelled":
            return
        if r.get("cleanup_pending"):
            st.update(status="cleanup_pending",
                      cleanup_target="cancelled" if st.get("status") == "cancelling" else st.get("cleanup_target", "failed"),
                      cleanup_error=r["cleanup_error"], error=r["error"],
                      timed_out=r.get("timed_out", False), usage=r["usage"])
            st.pop("finished", None)
            Path(_job_path(r["jid"], "result.txt")).write_text("CLEANUP PENDING: " + r["error"])
            return
        if st.get("status") in ("cancelling", "cleanup_pending"):
            return
        Path(_job_path(r["jid"], "result.txt")).write_text(r["text"] if r["ok"] else "FAILED: " + r["error"])
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
            with open(_job_path(jid, "log.txt"), "w") as log:
                pr = subprocess.Popen([sys.executable, os.path.realpath(__file__), "_job", jid],
                                      stdout=log, stderr=log, stdin=subprocess.DEVNULL, start_new_session=True,
                                      env={**os.environ, "CODEX_BRIDGE_HOME": BRIDGE_HOME})
            st["pid"] = pr.pid
    except OSError as exc:
        finish_run({"jid": jid, "ok": False, "text": "", "error": f"cannot launch worker: {exc}", "usage": None})
        sys.exit(f"cannot launch worker: {exc}")
    print(f"started background job {jid} ({kind}, {cfg['model']} {cfg['effort']}).")
    for label, command in (("watch live", "watch"), ("transcript", "log"), ("result", "result"),
                           ("wait", "wait"), ("cancel", "cancel"), ("take over", "attach")):
        print(f"  {label:<10}: {bridge_command(command, jid)}")



def cmd__job(a):
    jid = a.jid
    try:
        spec = json.loads(Path(_job_path(jid, "spec.json")).read_text())
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
                    target = "cancelled" if st["status"] == "cancelling" else st.get("cleanup_target", "failed")
                    st.update(status=target, finished=time.time())
                    if target == "failed":
                        Path(_job_path(jid, "result.txt")).write_text("FAILED: " + st.get("error", "cleanup completed"))
                else:
                    st.update(status="failed", error="worker process died")
        return dict(st)


def compact_state(st):
    return {k: st.get(k) for k in ("id", "status", "name", "thread", "model", "effort", "sandbox", "digest", "error")}


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
              f"{st.get('model','')+' '+st.get('effort',''):<22}{st.get('title','')}")


def cmd_result(a):
    jid = _resolve_job(a.job, getattr(a, "name", None)); st = _refresh(jid)
    if st["status"] in ACTIVE:
        print(f"job {jid} still running"); return
    print(Path(_job_path(jid, "result.txt")).read_text() if os.path.exists(_job_path(jid, "result.txt"))
          else f"job {jid}: {st['status']} ({st.get('error','no output')})")
    print(f"\n[job {jid} {st['status']} session={st.get('thread')}]", file=sys.stderr)


def cmd_wait(a):
    import time
    jid = _resolve_job(a.job, getattr(a, "name", None)); t0 = time.time()
    while _refresh(jid)["status"] in ACTIVE:
        if time.time() - t0 > a.timeout:
            print(f"job {jid} still running after {a.timeout}s"); return
        time.sleep(3)
    cmd_result(argparse.Namespace(job=jid))


def cmd_cancel(a):
    jid = _resolve_job(a.job, getattr(a, "name", None))
    with job_state(jid) as st:
        if st.get("status") not in ACTIVE:
            print(f"job {jid} is {st.get('status')}")
            return
        st["status"] = "cancelling"
        st["cleanup_target"] = "cancelled"
        st.setdefault("cancel_reason", "cancel requested")
        pgid = st.get("pgid")
        legacy = "pgid" not in st
    try:
        if legacy:
            raise OSError("no execution process group recorded for this legacy job; cannot safely confirm cancellation")
        if pgid is not None:
            terminate_group(pgid, jid=jid)
    except OSError as exc:
        with job_state(jid) as st:
            st.update(status="cleanup_pending", cleanup_target="cancelled", cleanup_error=str(exc),
                      error=(st.get("error", "") + f"\ncancellation failed: {exc}").strip())
        sys.exit(f"cancellation failed for {jid}: {exc}; status remains cleanup_pending")
    with job_state(jid) as st:
        st.update(status="cancelled", finished=time.time())
    print(f"cancelled {jid}")


def _stream(jid, follow, full=False, since=0, tail=None):
    if since < 0 or (tail is not None and tail < 0):
        sys.exit("--since and --tail must be nonnegative")
    path = _job_path(jid, "events.jsonl")
    if follow:
        while not os.path.exists(path) and _refresh(jid).get("status") in ACTIVE:
            time.sleep(0.2)
    if not os.path.exists(path):
        print("next cursor: 0")
        return
    st = _jread(jid) or {}
    if follow:
        print(f"═ codex run {jid} · {st.get('model')} {st.get('effort')} · {st.get('cwd')}", flush=True)
    cursor = 0
    with open(path) as f:
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
        print(f"═ {st.get('status')} · {st.get('digest', '')}")
    print(f"next cursor: {cursor}", flush=True)


def cmd_watch(a):
    _stream(_resolve_job(a.job, getattr(a, "name", None)), follow=True)


def cmd_log(a):
    _stream(_resolve_job(a.job, getattr(a, "name", None)), follow=False, full=a.full,
            since=getattr(a, "since", 0), tail=getattr(a, "tail", None))


def cmd_attach(a):
    jid = _resolve_job(a.job, getattr(a, "name", None)); st = _refresh(jid)
    if st.get("status") in ACTIVE and not getattr(a, "force", False):
        sys.exit(f"job {jid} is still {st['status']}; cancel/wait first, or use attach --force")
    if not st.get("thread"):
        sys.exit("no codex session id recorded yet for this run")
    cmd = [codex_bin()]
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
    raw = sys.stdin.read() if a.file == "-" else Path(a.file).read_text()
    tasks = json.loads(raw)
    s = load()

    if not isinstance(tasks, list) or not tasks:
        sys.exit("parallel expects a nonempty JSON task list")
    configs = []
    for task in tasks:
        ns = argparse.Namespace(**{k: task.get(k) for k in ("role", "model", "effort", "sandbox", "profile", "timeout")})
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
        print(f"===== {name} {header(r['cfg'], role)} session={r['thread']} =====")
        print(r["text"] if r["ok"] else f"FAILED: {r['error']}")
        print(f"  [activity: {r['digest']} · run {r['jid']}]")
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
    fd, tmp = tempfile.mkstemp(dir=directory, prefix=".link-")
    os.close(fd)
    os.unlink(tmp)
    try:
        os.symlink(target, tmp)
        os.replace(tmp, os.path.join(directory, "codex_bridge.py"))
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
    rv = sub.add_parser("review"); rv.add_argument("focus", nargs="*")
    g = rv.add_mutually_exclusive_group(); g.add_argument("--base"); g.add_argument("--commit")
    rv.add_argument("--adversarial", action="store_true"); rv.add_argument("--background", action="store_true")
    for x in ("model", "effort", "cd", "timeout", "profile"):
        rv.add_argument("--" + x, type=int if x == "timeout" else None)
    jb = sub.add_parser("jobs"); jb.add_argument("--limit", type=int, default=15); jb.add_argument("--name"); jb.add_argument("--json", action="store_true")
    for n in ("watch", "log", "attach"):
        q = sub.add_parser(n); q.add_argument("job", nargs="?", default="last"); q.add_argument("--name")
        if n == "log":
            q.add_argument("--full", action="store_true")
            q.add_argument("--since", type=int, default=0); q.add_argument("--tail", type=int)
        if n == "attach": q.add_argument("--force", action="store_true")
    for n in ("result", "cancel", "wait"):
        q = sub.add_parser(n); q.add_argument("job", nargs="?", default="last"); q.add_argument("--name")
        if n == "wait": q.add_argument("--timeout", type=int, default=600)
    sub.add_parser("_job").add_argument("jid")
    pl = sub.add_parser("parallel"); pl.add_argument("file", help="JSON task list file or -")
    pl.add_argument("--max", type=int)
    a = ap.parse_args()
    {"config": cmd_config, "models": cmd_models, "status": cmd_status, "roles": cmd_roles, "usage": cmd_usage, "parallel": cmd_parallel,
        "review": cmd_review, "watch": cmd_watch, "log": cmd_log, "attach": cmd_attach, "jobs": cmd_jobs, "result": cmd_result, "wait": cmd_wait, "cancel": cmd_cancel, "_job": cmd__job}.get(
        a.cmd, lambda x: run_codex(x, a.cmd == "resume"))(a)


if __name__ == "__main__":
    main()
