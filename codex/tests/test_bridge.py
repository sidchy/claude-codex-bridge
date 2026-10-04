import argparse
import contextlib
import importlib.util
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

SCRIPT = Path(__file__).resolve().parents[1] / "scripts/codex_bridge.py"
spec = importlib.util.spec_from_file_location("bridge", SCRIPT)
b = importlib.util.module_from_spec(spec)
spec.loader.exec_module(b)


class BridgeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.home = self.root / "bridge"
        self.home.mkdir()
        self.cwd = self.root / "repo"
        self.cwd.mkdir()
        for key, value in {"BRIDGE_HOME": str(self.home), "SETTINGS": str(self.home / "settings.json"), "JOBS": str(self.home / "jobs")}.items():
            patch = mock.patch.object(b, key, value)
            patch.start()
            self.addCleanup(patch.stop)
        self.env = {**os.environ, "CODEX_BRIDGE_HOME": str(self.home), "CODEX_HOME": str(self.root / "native")}
        # Directly executable bridge/fake scripts must use this suite's interpreter too.
        self.env["PATH"] = os.path.dirname(sys.executable) + os.pathsep + os.environ["PATH"]
        native_patch = mock.patch.dict(os.environ, {"CODEX_HOME": self.env["CODEX_HOME"], "CODEX_BRIDGE_HOME": str(self.home), "PATH": self.env["PATH"]})
        native_patch.start()
        self.addCleanup(native_patch.stop)
        self.catalog = [{"slug": model, "visibility": "list", "supported_reasoning_levels": [{"effort": effort} for effort in ("low", "medium", "high")]} for model in ("gpt-6.1-sol", "gpt-6-astra", "gpt-6-luna")]
        self.catalog_patch = mock.patch.object(b, "model_catalog", return_value=self.catalog)
        self.catalog_patch.start()
        self.addCleanup(self.catalog_patch.stop)

    def cfg(self, **kw):
        return {**b.DEFAULTS, "preamble": "", **kw}

    def args(self, **kw):
        return argparse.Namespace(cd=str(self.cwd), prompt="hello", background=False, add_dir=None, **kw)

    def fake(self, code, complete=True):
        binary = self.root / "fake codex"
        binary.write_text("#!/usr/bin/env python3\nimport sys,json\nif 'debug' in sys.argv and 'models' in sys.argv:\n print(" + repr(json.dumps({"models": self.catalog})) + "); sys.exit(0)\n" + code)
        if complete:
            with binary.open("a") as f:
                f.write("\nprint(json.dumps({'type':'turn.completed'}))\n")
        binary.chmod(0o755)
        b.save({**b.load(), "codex_bin": str(binary)})
        return binary

    def test_role_sandbox_settings(self):
        for role in ("worker", "debugger"):
            self.assertEqual(b.resolve(self.args(role=role), self.cfg(sandbox="workspace-write"))["sandbox"], "workspace-write")
        for role in ("explorer", "reviewer", "architect"):
            self.assertEqual(b.resolve(self.args(role=role), self.cfg())["sandbox"], "read-only")
            self.assertEqual(b.resolve(self.args(role=role, sandbox="workspace-write"), self.cfg())["sandbox"], "workspace-write")

    def test_review_sandbox_and_resume_inheritance(self):
        capture = self.root / "args.json"
        self.fake("import sys,json\nfrom pathlib import Path\nPath(" + repr(str(capture)) + ").write_text(json.dumps(sys.argv))\nprint(json.dumps({'type':'item.completed','item':{'type':'agent_message','text':'ok'}}))\n")
        r = b.exec_codex(self.cfg(sandbox="read-only"), None, str(self.cwd), review=["--uncommitted"])
        self.assertIn('sandbox_mode="read-only"', json.loads(capture.read_text()))
        self.assertEqual(b._jread(r["jid"])["sandbox"], "read-only")
        b._jwrite(r["jid"], thread="thread-1", kind="task", status="done")
        with mock.patch.object(b, "exec_codex", return_value=r) as execute, contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()) as err:
            b.run_codex(self.args(cont=True, sandbox="danger-full-access"), False)
        self.assertEqual(execute.call_args.args[0]["sandbox"], "read-only")
        self.assertIn("inherits", err.getvalue())

    def hanging_fake(self, detached=False):
        self.ready = self.root / "child-ready"
        child = "import os,signal,time; from pathlib import Path; signal.signal(signal.SIGTERM,signal.SIG_IGN); Path(" + repr(str(self.ready)) + ").write_text(str(os.getpid())); time.sleep(60)"
        if detached:
            child = "import subprocess,sys,time; subprocess.Popen([sys.executable, '-c', " + repr(child) + "], start_new_session=True); time.sleep(60)"
        return self.fake("import os,signal,subprocess,sys,time\n"
                         "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
                         "subprocess.Popen([sys.executable,'-c'," + repr(child) + "])\n"
                         "print('ready',flush=True)\ntime.sleep(60)\n")

    def wait_ready(self):
        import time
        deadline = time.monotonic() + 5
        while not self.ready.exists() and time.monotonic() < deadline:
            time.sleep(0.02)
        self.assertTrue(self.ready.exists(), "fake descendant never started")

    def test_timeout_kills_descendant_group(self):
        import time
        self.hanging_fake()
        start = time.monotonic()
        r = b.exec_codex(self.cfg(timeout=1.0), "hi", str(self.cwd))
        b.finish_run(r)
        self.assertLess(time.monotonic() - start, 5)
        self.assertTrue(self.ready.exists(), "timeout happened before descendant started")
        self.assertIn("timed out", r["error"])
        self.assertFalse(b.group_alive(b._jread(r["jid"])["pgid"]))
        self.assertEqual(b._jread(r["jid"])["status"], "failed")

    def test_cancel_group_and_finish_race(self):
        import time
        from concurrent.futures import ThreadPoolExecutor
        self.hanging_fake()
        jid = b.new_run("task", self.cfg(), "hi", str(self.cwd))
        with ThreadPoolExecutor(1) as pool:
            future = pool.submit(b.exec_codex, self.cfg(timeout=10), "hi", str(self.cwd), jid=jid)
            deadline = time.monotonic() + 5
            while not b._jread(jid).get("pgid") and time.monotonic() < deadline:
                time.sleep(0.01)
            self.wait_ready()
            with contextlib.redirect_stdout(io.StringIO()):
                b.cmd_cancel(argparse.Namespace(job=jid))
            r = future.result(timeout=5)
        b.finish_run(r)
        self.assertEqual(b._jread(jid)["status"], "cancelled")
        self.assertFalse(b.group_alive(b._jread(jid)["pgid"]))
        self.assertFalse(Path(b._job_path(jid, "result.txt")).exists())

    def test_cancel_failure_is_not_success(self):
        jid = b.new_run("task", self.cfg(), "hi", str(self.cwd))
        b._jwrite(jid, pgid=999999)
        with mock.patch.object(b, "terminate_group", side_effect=PermissionError("denied")), self.assertRaisesRegex(SystemExit, "cancellation failed"):
            b.cmd_cancel(argparse.Namespace(job=jid))
        self.assertEqual(b._jread(jid)["status"], "cleanup_pending")
        b.finish_run({"jid": jid, "ok": True, "text": "ok", "error": "", "usage": None})
        self.assertEqual(b._jread(jid)["status"], "cleanup_pending")

    def test_spawn_failure_is_recorded(self):
        self.fake("pass\n")
        with mock.patch.object(b.subprocess, "Popen", side_effect=OSError("cannot launch")):
            r = b.exec_codex(self.cfg(), "hi", str(self.cwd))
        b.finish_run(r)
        self.assertEqual(b._jread(r["jid"])["status"], "failed")
        self.assertIn("cannot launch", r["error"])

    def test_settings_corrupt_unreadable_and_atomic_updates(self):
        self.assertEqual(b.load()["sandbox"], "danger-full-access")
        Path(b.SETTINGS).write_text("{")
        with self.assertRaisesRegex(SystemExit, "settings.json"):
            b.load()
        with self.assertRaisesRegex(SystemExit, "settings.json"):
            b.cmd_config(argparse.Namespace(action="set", pairs=["effort=low"]))
        self.assertEqual(Path(b.SETTINGS).read_text(), "{")
        Path(b.SETTINGS).unlink()
        Path(b.SETTINGS).mkdir()
        with self.assertRaisesRegex(SystemExit, "settings.json"):
            b.load()
        Path(b.SETTINGS).rmdir()
        pairs = ["model=one", "effort=low", "sandbox=read-only", "profile=custom"]
        procs = [subprocess.Popen([sys.executable, str(SCRIPT), "config", "set", pair], env=self.env, stdout=subprocess.PIPE, stderr=subprocess.PIPE) for pair in pairs]
        for proc in procs:
            out, err = proc.communicate(timeout=10)
            self.assertEqual(proc.returncode, 0, err)
        for pair in pairs:
            k, v = pair.split("=")
            self.assertEqual(b.load()[k], v)
        original = Path(b.SETTINGS).read_bytes()
        with mock.patch.object(b.os, "replace", side_effect=OSError("interrupted")), self.assertRaises(OSError):
            b.save(self.cfg())
        self.assertEqual(Path(b.SETTINGS).read_bytes(), original)

    def test_named_session_atomic_placeholder(self):
        from concurrent.futures import ThreadPoolExecutor
        def launch():
            try:
                b.run_codex(self.args(name="shared"), False)
                return "started"
            except SystemExit as exc:
                return str(exc)
        # Hold execution without a thread.started event: the placeholder alone
        # must prevent another launch, including through a symlink cwd.
        def execute(cfg, prompt, *args, **kwargs):
            return {"jid": kwargs["jid"], "ok": True, "text": "ok", "error": "", "usage": None, "thread": "t", "digest": ""}
        with mock.patch.object(b, "exec_codex", side_effect=execute), mock.patch.object(b, "finish_run"), contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            with ThreadPoolExecutor(2) as pool:
                results = list(pool.map(lambda _: launch(), range(2)))
        self.assertEqual(results.count("started"), 1)
        self.assertTrue(any("already has active job" in x for x in results))
        self.assertEqual(len(list(Path(b.JOBS).iterdir())), 1)
        alias = self.root / "alias"
        alias.symlink_to(self.cwd, target_is_directory=True)
        args = self.args(name="shared")
        args.cd = str(alias)
        with self.assertRaisesRegex(SystemExit, "already has active job"):
            b.run_codex(args, False)

    def test_supervision_local_name_and_global_notice(self):
        local = b.new_run("task", self.cfg(), "local", str(self.cwd), name="keep")
        other = b.new_run("task", self.cfg(), "other", str(self.root), name="keep")
        with mock.patch.object(b.os, "getcwd", return_value=str(self.cwd)):
            self.assertEqual(b._resolve_job(), local)
            self.assertEqual(b._resolve_job(name="keep"), local)
            self.assertEqual(b._resolve_job(other), other)
            for command in ("watch", "log", "result", "wait", "cancel", "attach", "jobs"):
                out = subprocess.run([sys.executable, str(SCRIPT), command, "--help"], env=self.env, capture_output=True, text=True)
                self.assertIn("--name", out.stdout)
        empty = self.root / "empty"
        empty.mkdir()
        with mock.patch.object(b.os, "getcwd", return_value=str(empty)), contextlib.redirect_stderr(io.StringIO()) as err:
            self.assertIn(b._resolve_job(), (local, other))
        self.assertIn("falling back to global", err.getvalue())

    def test_all_resume_entry_points_lock_metadata(self):
        jid = b.new_run("task", self.cfg(model="gpt-6-astra", effort="high", sandbox="read-only", profile="original"), "hi", str(self.cwd), name="label")
        b._jwrite(jid, thread="session-id", status="done")
        variants = [(False, {"cont": True}), (False, {"name": "label"}),
                    (True, {}), (True, {"session": "session-id"})]
        for resume, flags in variants:
            with self.subTest(flags=flags), contextlib.redirect_stderr(io.StringIO()):
                cfg, _, session, resumed, label, new = b.prepare_run(self.args(model="gpt-6-luna", effort="low", sandbox="workspace-write", **flags), resume)
                self.assertEqual((cfg["model"], cfg["effort"], cfg["sandbox"], cfg["profile"]), ("gpt-6-astra", "high", "read-only", "original"))
                self.assertEqual(session, "session-id")
                self.assertTrue(resumed)
                b._jwrite(new, status="done")
        with contextlib.redirect_stderr(io.StringIO()) as err:
            cfg, _, session, resumed, _, _ = b.prepare_run(self.args(session="unknown"), True)
        self.assertIn("WARNING", err.getvalue())
        self.assertEqual(cfg["sandbox"], b.DEFAULTS["sandbox"])

    def test_attach_quotes_profile_and_refuses_running(self):
        import shlex
        weird = self.root / "space ' $(touch nope)"
        weird.mkdir()
        binary = self.fake("pass\n")
        jid = b.new_run("task", self.cfg(profile="my profile"), "hi", str(weird))
        b._jwrite(jid, thread="thread ' special")
        with contextlib.redirect_stdout(io.StringIO()) as out, self.assertRaisesRegex(SystemExit, "still running"):
            b.cmd_attach(argparse.Namespace(job=jid, force=False))
        self.assertEqual(out.getvalue(), "")
        for status, force in (("running", True), ("done", False)):
            b._jwrite(jid, status=status)
            with contextlib.redirect_stdout(io.StringIO()) as out, contextlib.redirect_stderr(io.StringIO()):
                b.cmd_attach(argparse.Namespace(job=jid, force=force))
            tokens = shlex.split(out.getvalue())
            self.assertEqual(tokens[:7], ["cd", os.path.realpath(weird), "&&", str(binary), "-p", "my profile", "resume"])
            self.assertEqual(tokens[7], "thread ' special")

    def test_raw_output_preserved_and_failure_tail_bounded(self):
        self.fake("import sys\nfor i in range(100): print(str(i)+':'+('x'*500))\nsys.stdout.flush(); print('invalid flag: --bad',file=sys.stderr)\nsys.exit(2)\n")
        r = b.exec_codex(self.cfg(), "hi", str(self.cwd))
        events = [json.loads(line) for line in Path(b._job_path(r["jid"], "events.jsonl")).read_text().splitlines()]
        self.assertEqual(len(events), 101)
        self.assertTrue(all(ev["type"] == "raw" for ev in events))
        self.assertIn("invalid flag: --bad", r["error"])
        self.assertLess(len(r["error"]), 4100)
        self.assertTrue(any(ev["text"].startswith("0:") for ev in events))

    def test_incremental_log_tail_and_compact_status(self):
        jid = b.new_run("task", self.cfg(), "hi", str(self.cwd))
        path = Path(b._job_path(jid, "events.jsonl"))
        path.write_text("".join(json.dumps({"type": "raw", "text": f"line{i}"}) + "\n" for i in range(3)))
        def log(**kw):
            with contextlib.redirect_stdout(io.StringIO()) as out:
                b.cmd_log(argparse.Namespace(job=jid, full=False, **kw))
            return out.getvalue()
        self.assertIn("next cursor: 3", log(since=0))
        self.assertEqual(log(since=3), "next cursor: 3\n")
        self.assertEqual(log(tail=1), "line2\nnext cursor: 3\n")
        with path.open("a") as f:
            f.write(json.dumps({"type": "raw", "text": "new"}) + "\n")
        self.assertEqual(log(since=3), "new\nnext cursor: 4\n")
        result = subprocess.run([sys.executable, str(SCRIPT), "status", jid, "--json"], env=self.env, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)["id"], jid)
        result = subprocess.run([sys.executable, str(SCRIPT), "jobs", "--json"], cwd=self.cwd, env=self.env, capture_output=True, text=True)
        self.assertEqual(json.loads(result.stdout)[0]["id"], jid)

    def test_digest_hashes_already_modified_and_caps(self):
        def git(*args):
            subprocess.run(["git", *args], cwd=self.cwd, check=True, capture_output=True)
        git("init")
        target = self.cwd / "dirty.py"
        target.write_text("original")
        git("add", ".")
        git("-c", "user.name=Test", "-c", "user.email=test@example.invalid", "commit", "-m", "initial")
        target.write_text("before")
        self.fake("import json\nfrom pathlib import Path\nPath('dirty.py').write_text('after')\nPath('new file').write_text('new')\nprint(json.dumps({'type':'item.completed','item':{'type':'agent_message','text':'ok'}}))\n")
        r = b.exec_codex(self.cfg(), "hi", str(self.cwd))
        self.assertIn("files touched: dirty.py, new file", r["digest"])
        with mock.patch.object(b, "HASH_FILE_LIMIT", 1):
            r = b.exec_codex(self.cfg(), "hi", str(self.cwd))
        self.assertIn("hash snapshot capped", r["digest"])
        self.assertNotIn("files changed", r["digest"])

    def test_latest_timestamp_and_realpath(self):
        for jid, started in (("1231-old", 100.1), ("0101-new", 100.2), ("0101-aaa", 100.3)):
            Path(b.JOBS, jid).mkdir(parents=True)
            b._jwrite(jid, id=jid, started=started, cwd=str(self.cwd), thread=jid, kind="task", name="label")
        alias = self.root / "alias"
        alias.symlink_to(self.cwd, target_is_directory=True)
        with mock.patch.object(b.os, "getcwd", return_value=str(alias)):
            self.assertEqual(b._resolve_job(), "0101-aaa")
        self.assertEqual(b.find_session(cwd=str(alias))["thread"], "0101-aaa")
        self.assertEqual(b.find_session(cwd=str(alias), name="label")["thread"], "0101-aaa")
        jid = b.new_run("task", self.cfg(), "hi", str(alias))
        self.assertEqual(b._jread(jid)["cwd"], os.path.realpath(self.cwd))

    def test_catalog_validation_cache_and_parallel_limit(self):
        self.fake("pass\n")
        self.catalog_patch.stop()
        b.validate_model(self.cfg())
        cache = self.home / "models.json"
        self.assertTrue(cache.exists())
        with mock.patch.object(b.subprocess, "run", side_effect=AssertionError("cache missed")):
            b.validate_model(self.cfg())
            with self.assertRaisesRegex(SystemExit, "unknown model"):
                b.validate_model(self.cfg(model="bad"))
            with self.assertRaisesRegex(SystemExit, "does not support"):
                b.validate_model(self.cfg(effort="ultra"))
        data = json.loads(cache.read_text())
        data["fetched"] -= 3601
        cache.write_text(json.dumps(data))
        with mock.patch.object(b.subprocess, "run", wraps=subprocess.run) as run:
            b.validate_model(self.cfg())
            self.assertEqual(run.call_count, 1)
        b.save({**b.load(), "max_parallel": 2})
        tasks = self.root / "tasks.json"
        tasks.write_text(json.dumps([{"prompt": "hi"}] * 5))
        with mock.patch("concurrent.futures.ThreadPoolExecutor") as pool, contextlib.redirect_stdout(io.StringIO()), self.assertRaises(SystemExit) as stop:
            pool.return_value.__enter__.return_value.map.return_value = []
            b.cmd_parallel(argparse.Namespace(file=str(tasks), max=None))
        self.assertEqual(stop.exception.code, 0)
        pool.assert_called_once_with(max_workers=2)

    def test_stable_executable_symlink_and_prompt(self):
        result = subprocess.run([str(SCRIPT), "--help"], env=self.env, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        link = self.home / "bin/codex_bridge.py"
        self.assertTrue(link.is_symlink())
        self.assertEqual(link.resolve(), SCRIPT)
        link.unlink()
        link.symlink_to(self.root / "stale")
        result = subprocess.run([str(SCRIPT), "--help"], env=self.env, capture_output=True, text=True)
        self.assertEqual(link.resolve(), SCRIPT)
        self.assertTrue(os.access(link, os.X_OK))
        doc = (SCRIPT.parents[1] / "commands/run.md").read_text()
        self.assertIn('"${CLAUDE_PLUGIN_ROOT}/scripts/codex_bridge.py" run', doc)
        self.assertIn("bin/codex_bridge.py watch <id>", doc)
        self.assertIn("No terminal tool", doc)
        self.assertIn("--since", doc)
        self.assertNotIn("zsh exit 127", doc)

    def test_structured_config_validates_without_writing_bad_values(self):
        good = ['roles={"worker":{"model":"gpt-6-astra","effort":"high","preamble":"exact","sandbox":"workspace-write"}}', 'extra_args=["--add-dir","space dir"]']
        with contextlib.redirect_stdout(io.StringIO()):
            b.cmd_config(argparse.Namespace(action="set", pairs=good))
            b.cmd_roles(None)
        self.assertIsInstance(b.load()["roles"], dict)
        self.assertEqual(b.load()["extra_args"], ["--add-dir", "space dir"])
        self.assertEqual(b.validate("extra_args", '--add-dir "space dir"'), ["--add-dir", "space dir"])
        original = Path(b.SETTINGS).read_bytes()
        for bad in ('roles="string"', 'roles=[]', 'roles={"x":1}', 'roles={"x":{"sandbox":"bad"}}', 'roles={"x":{"effort":"bad"}}', 'roles={"x":{"model":3}}', 'roles={"x":{"preamble":false}}', 'extra_args=[1]', 'max_parallel=0'):
            with self.subTest(bad=bad), self.assertRaises(SystemExit):
                b.cmd_config(argparse.Namespace(action="set", pairs=["effort=low", bad]))
            self.assertEqual(Path(b.SETTINGS).read_bytes(), original)
        with contextlib.redirect_stdout(io.StringIO()) as out:
            b.cmd_config(argparse.Namespace(action="show"))
        for key in ("model_reasoning", "model_bulk", "review_model", "review_effort", "team_policy", "max_parallel"):
            self.assertIn(key, json.loads(out.getvalue()))
        cfg = self.cfg(roles={"explorer": {"sandbox": "danger-full-access"}})
        self.assertEqual(b.resolve(self.args(role="explorer"), cfg)["sandbox"], "read-only")

    def test_nonfatal_cli_warning_with_successful_result(self):
        self.fake("print(json.dumps({'type':'item.completed','item':{'type':'error','message':'Codex is ignoring 1 unrecognized configuration setting'}}))\nprint(json.dumps({'type':'item.completed','item':{'type':'agent_message','text':'ok'}}))\n")
        r = b.exec_codex(self.cfg(), "hi", str(self.cwd))
        self.assertTrue(r["ok"], r["error"])
        self.assertEqual(r["text"], "ok")

    def test_resume_new_name_creates_instead_of_native_last(self):
        with contextlib.redirect_stderr(io.StringIO()):
            _, _, session, resumed, _, _ = b.prepare_run(self.args(name="new-label"), True)
        self.assertIsNone(session)
        self.assertFalse(resumed)

    def test_background_and_parallel_cancel_actual_processes(self):
        import time
        self.hanging_fake(detached=True)
        def cli(*args):
            return subprocess.run([str(SCRIPT), *args], cwd=self.cwd, env=self.env,
                                  capture_output=True, text=True, timeout=10)
        first = cli("run", "--name", "background", "--background", "hi")
        self.assertEqual(first.returncode, 0, first.stderr)
        self.wait_ready()
        jid = b.ordered_jobs()[0]
        second = cli("run", "--name", "background", "--background", "hi")
        self.assertNotEqual(second.returncode, 0)
        self.assertIn("already has active job", second.stderr)
        cancelled = cli("cancel", jid)
        self.assertEqual(cancelled.returncode, 0, cancelled.stderr)
        self.assertEqual(b._jread(jid)["status"], "cancelled")
        self.assertFalse(b.group_alive(b._jread(jid)["pgid"]))
        self.assert_dead(int(self.ready.read_text()))
        self.ready.unlink()
        taskfile = self.root / "parallel.json"
        taskfile.write_text(json.dumps([{"prompt": "hi", "cd": str(self.cwd)}]))
        proc = subprocess.Popen([str(SCRIPT), "parallel", str(taskfile)], env=self.env, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        try:
            self.wait_ready()
            parallel_jid = b.ordered_jobs()[0]
            self.assertNotEqual(parallel_jid, jid)
            cancelled = cli("cancel", parallel_jid)
            self.assertEqual(cancelled.returncode, 0, cancelled.stderr)
            out, err = proc.communicate(timeout=10)
            self.assertEqual(proc.returncode, 1, err)
            self.assertEqual(b._jread(parallel_jid)["status"], "cancelled")
            self.assertFalse(b.group_alive(b._jread(parallel_jid)["pgid"]))
            self.assert_dead(int(self.ready.read_text()))
        finally:
            if proc.poll() is None:
                proc.kill()
            proc.communicate()

    def test_cancel_before_launch_and_background_start_failure(self):
        jid = b.new_run("task", self.cfg(), "hi", str(self.cwd))
        with contextlib.redirect_stdout(io.StringIO()):
            b.cmd_cancel(argparse.Namespace(job=jid))
            with mock.patch.object(b.subprocess, "Popen") as popen:
                b.start_job("task", self.cfg(), "hi", str(self.cwd), jid=jid)
                popen.assert_not_called()
        self.assertEqual(b._jread(jid)["status"], "cancelled")
        other = b.new_run("task", self.cfg(), "hi", str(self.cwd))
        with mock.patch.object(b.subprocess, "Popen", side_effect=OSError("worker launch denied")), self.assertRaisesRegex(SystemExit, "worker launch denied"):
            b.start_job("task", self.cfg(), "hi", str(self.cwd), jid=other)
        self.assertEqual(b._jread(other)["status"], "failed")
        self.assertIn("worker launch denied", Path(b._job_path(other, "result.txt")).read_text())
        with mock.patch.object(b, "exec_codex", side_effect=SystemExit("bad catalog")):
            b.cmd__job(argparse.Namespace(jid=other))
        self.assertIn("bad catalog", b._jread(other)["error"])

    def test_cancel_legacy_job_without_group_does_not_claim_success(self):
        jid = b.new_run("task", self.cfg(), "hi", str(self.cwd))
        with b.job_state(jid) as st:
            del st["pgid"]
        with self.assertRaisesRegex(SystemExit, "no execution process group"):
            b.cmd_cancel(argparse.Namespace(job=jid))
        self.assertEqual(b._jread(jid)["status"], "cleanup_pending")

    def test_r1_resume_passes_recorded_sandbox(self):
        capture = self.root / "resume-argv.json"
        self.fake("from pathlib import Path\nPath(" + repr(str(capture)) + ").write_text(json.dumps(sys.argv))\nprint(json.dumps({'type':'item.completed','item':{'type':'agent_message','text':'ok'}}))\n")
        for sandbox in ("danger-full-access", "read-only", "unknown"):
            with self.subTest(sandbox=sandbox):
                r = b.exec_codex(self.cfg(sandbox=sandbox), "hi", str(self.cwd), session="recorded", resume=True)
                self.assertTrue(r["ok"], r["error"])
                expected = b.DEFAULTS["sandbox"] if sandbox == "unknown" else sandbox
                argv = json.loads(capture.read_text())
                self.assertIn('sandbox_mode="' + expected + '"', argv)
                self.assertIn('approval_policy="never"', argv)
                self.assertEqual(b._jread(r["jid"])["sandbox"], expected)

    def assert_dead(self, pid):
        snapshot = b.process_snapshot()
        self.assertIsNotNone(snapshot)
        self.assertTrue(pid not in snapshot or snapshot[pid][2].startswith("Z"), f"surviving process {pid}")

    def test_r2_detached_descendants_timeout_and_cancel(self):
        import time
        from concurrent.futures import ThreadPoolExecutor
        self.hanging_fake(detached=True)
        r = b.exec_codex(self.cfg(timeout=1), "hi", str(self.cwd))
        self.assertIn("timed out", r["error"])
        descendant = int(self.ready.read_text())
        self.assert_dead(descendant)
        self.assertNotIn(descendant, b._jread(r["jid"])["tracked_pids"])
        self.ready.unlink()
        jid = b.new_run("task", self.cfg(), "hi", str(self.cwd))
        with ThreadPoolExecutor(1) as pool:
            future = pool.submit(b.exec_codex, self.cfg(timeout=10), "hi", str(self.cwd), jid=jid)
            self.wait_ready()
            descendant = int(self.ready.read_text())
            self.assertNotEqual(os.getpgid(descendant), b._jread(jid)["pgid"])
            with contextlib.redirect_stdout(io.StringIO()):
                b.cmd_cancel(argparse.Namespace(job=jid))
            future.result(timeout=8)
        self.assert_dead(descendant)
        self.assertEqual(b._jread(jid)["status"], "cancelled")

    def test_r3_ps_failure_still_kills_and_bounds_reads(self):
        import signal
        import time
        real_run = subprocess.run
        for failure in (FileNotFoundError("ps missing"), PermissionError("ps blocked"), subprocess.TimeoutExpired("ps", 0.5)):
            with self.subTest(failure=type(failure).__name__):
                self.hanging_fake()
                def run(args, **kwargs):
                    if args[0] == "ps":
                        raise failure
                    return real_run(args, **kwargs)
                start = time.monotonic()
                with mock.patch.object(b.subprocess, "run", side_effect=run), mock.patch.object(b.os, "killpg", wraps=os.killpg) as kill:
                    r = b.exec_codex(self.cfg(timeout=1), "x" * 200000, str(self.cwd))
                self.assertLess(time.monotonic() - start, 5)
                self.assertIn("timed out", r["error"])
                self.assertTrue(any(call.args[1] == signal.SIGKILL for call in kill.call_args_list))
                self.assert_dead(b._jread(r["jid"])["child_pid"])
                self.assert_dead(int(self.ready.read_text()))

    def test_r3_failed_kill_has_bounded_final_wait(self):
        import time
        self.hanging_fake()
        start = time.monotonic()
        processes = []
        real_popen = subprocess.Popen
        def launch(*args, **kwargs):
            proc = real_popen(*args, **kwargs)
            processes.append(proc)
            return proc
        try:
            with mock.patch.object(b, "terminate_group", side_effect=PermissionError("kill blocked")), mock.patch.object(b.subprocess, "Popen", side_effect=launch):
                r = b.exec_codex(self.cfg(timeout=1), "hi", str(self.cwd))
            self.assertLess(time.monotonic() - start, 4)
            self.assertIn("cleanup failed", r["error"])
            self.assertFalse(r["ok"])
        finally:
            for jid in b.ordered_jobs():
                state = b._jread(jid)
                if state.get("pgid"):
                    b.terminate_group(state["pgid"], jid=jid)
            for proc in processes:
                proc.wait(timeout=2)

    def test_r4_cancelling_converges_and_unblocks_supervision(self):
        self.fake("pass\n")
        jid = b.new_run("task", self.cfg(), "hi", str(self.cwd), name="recover")
        b._jwrite(jid, status="cancelling", pid=99999999, pgid=99999998,
                  child_pid=99999998, thread="old-thread", error="cancel interrupted")
        with contextlib.redirect_stdout(io.StringIO()) as out, contextlib.redirect_stderr(io.StringIO()):
            b.cmd_wait(argparse.Namespace(job=jid, timeout=1))
            b.cmd_attach(argparse.Namespace(job=jid, force=False))
        state = b._jread(jid)
        self.assertEqual(state["status"], "cancelled")
        self.assertEqual(state["error"], "cancel interrupted")
        self.assertIn("resume", out.getvalue())
        with contextlib.redirect_stderr(io.StringIO()):
            cfg, _, session, resumed, _, new = b.prepare_run(self.args(name="recover"), False)
        self.assertEqual(session, "old-thread")
        self.assertTrue(resumed)
        # A still-running descendant prevents premature convergence.
        row = b.process_snapshot()[os.getpid()]
        b._jwrite(new, status="cancelling", worker_finished=True, tracked_processes={str(os.getpid()): {"identity": row[3], "pgid": row[1]}})
        self.assertEqual(b._refresh(new)["status"], "cancelling")
        b._jwrite(new, tracked_processes={})
        self.assertEqual(b._refresh(new)["status"], "cancelled")

    def test_r5_custom_models_and_profile_catalog_cache(self):
        for cfg in (self.cfg(model="deployment", profile="custom"), self.cfg(model="deployment", extra_args=["-c", 'model_provider="custom"'])):
            with contextlib.redirect_stderr(io.StringIO()) as err:
                b.validate_model(cfg)
            self.assertIn("WARNING", err.getvalue())
        with self.assertRaisesRegex(SystemExit, "unknown model"):
            b.validate_model(self.cfg(model="deployment"))
        with self.assertRaisesRegex(SystemExit, "does not support"):
            b.validate_model(self.cfg(profile="custom", effort="ultra"))
        native = Path(self.env["CODEX_HOME"])
        native.mkdir()
        (native / "config.toml").write_text('model_provider = "custom"')
        with contextlib.redirect_stderr(io.StringIO()):
            b.validate_model(self.cfg(model="deployment"))
        self.fake("pass\n")
        self.catalog_patch.stop()
        with mock.patch.object(b.subprocess, "run", wraps=subprocess.run) as run:
            b.model_catalog(self.cfg(profile="one"), str(self.cwd))
            b.model_catalog(self.cfg(profile="one"), str(self.cwd))
            b.model_catalog(self.cfg(profile="two"), str(self.cwd))
        self.assertEqual(run.call_count, 2)
        self.assertEqual(run.call_args_list[0].args[0][1:3], ["-p", "one"])
        self.assertEqual(run.call_args_list[1].args[0][1:3], ["-p", "two"])
        self.assertEqual(json.loads((self.home / "models.json").read_text())["context"]["profile"], "two")

    def test_r6_reset_preserves_corrupt_settings(self):
        Path(b.SETTINGS).write_text("{broken")
        for action in ("show", "set"):
            with self.assertRaisesRegex(SystemExit, "settings.json"):
                b.cmd_config(argparse.Namespace(action=action, pairs=["effort=low"]))
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()) as err:
            b.cmd_config(argparse.Namespace(action="reset"))
        self.assertEqual(b.load(), b.DEFAULTS)
        backups = list(self.home.glob("settings.json.corrupt-*"))
        self.assertEqual(len(backups), 1)
        self.assertEqual(backups[0].read_text(), "{broken")
        self.assertIn(str(backups[0]), err.getvalue())

    def test_r7_timeout_normalized_and_rejected_before_launch(self):
        self.assertEqual(b.resolve(self.args(timeout="5"), self.cfg())["timeout"], 5)
        for timeout in (0, -1, "0", "-1", "abc", 1.5, True):
            with self.subTest(timeout=timeout), self.assertRaisesRegex(SystemExit, "timeout.*positive integer"):
                b.resolve(self.args(timeout=timeout), self.cfg())
        result = subprocess.run([str(SCRIPT), "run", "--timeout", "-1", "hi"], env=self.env, capture_output=True, text=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("positive integer", result.stderr)
        self.assertFalse(Path(b.JOBS).exists())
        self.fake("print(json.dumps({'type':'item.completed','item':{'type':'agent_message','text':'ok'}}))\n")
        tasks = self.root / "timeout-tasks.json"
        tasks.write_text(json.dumps([{"prompt": "hi", "timeout": "5", "cd": str(self.cwd)}]))
        result = subprocess.run([str(SCRIPT), "parallel", str(tasks)], env=self.env, capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("ok", result.stdout)

    def test_r8_printed_commands_work_in_fresh_terminal(self):
        self.fake("print('interactive-resume-ok')\n")
        b.refresh_bridge_link()
        jid = b.new_run("task", self.cfg(), "hi", str(self.cwd))
        b._jwrite(jid, status="done", thread="recorded")
        Path(b._job_path(jid, "result.txt")).write_text("result-ok")
        Path(b._job_path(jid, "events.jsonl")).write_text(json.dumps({"type": "raw", "text": "event-ok"}) + "\n")
        fresh_env = {**self.env, "HOME": str(self.root / "fresh-home")}
        fresh_env.pop("CODEX_BRIDGE_HOME")
        for action in ("watch", "log", "attach", "result", "wait", "cancel", "jobs"):
            command = b.bridge_command(action, *([] if action == "jobs" else [jid]))
            self.assertTrue(command.startswith("CODEX_BRIDGE_HOME="))
            out = subprocess.run(command, shell=True, cwd=self.cwd, env=fresh_env, capture_output=True, text=True, timeout=5)
            self.assertEqual(out.returncode, 0, out.stderr)
            self.assertNotIn("no jobs", out.stdout + out.stderr)
        with contextlib.redirect_stdout(io.StringIO()) as out:
            b.print_footer({"jid": jid, "thread": "recorded", "digest": "test"})
        started = subprocess.run([str(SCRIPT), "run", "--background", "hi"], cwd=self.cwd, env=self.env, capture_output=True, text=True, timeout=5)
        self.assertEqual(started.returncode, 0, started.stderr)
        for line in (out.getvalue() + started.stdout).splitlines():
            if "bin/codex_bridge.py" in line:
                self.assertIn("CODEX_BRIDGE_HOME=", line)
        # Reap the short-lived background fixture before removing its home.
        import time
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            latest = b._refresh(b.ordered_jobs()[0])
            if latest["status"] not in b.ACTIVE:
                break
            time.sleep(0.05)
        self.assertNotIn(latest["status"], b.ACTIVE)

    def test_f1_recoverable_error_and_terminal_failures(self):
        cases = [
            ([{"type": "error", "message": "Reconnecting..."}, {"type": "item.completed", "item": {"type": "agent_message", "text": "ok"}}, {"type": "turn.completed"}], 0, True),
            ([{"type": "turn.failed", "error": "terminal"}, {"type": "item.completed", "item": {"type": "agent_message", "text": "partial"}}, {"type": "turn.completed"}], 0, False),
            ([{"type": "error", "message": "disconnected"}], 1, False),
            ([{"type": "item.completed", "item": {"type": "agent_message", "text": "incomplete"}}], 0, False),
        ]
        for events, code, expected in cases:
            with self.subTest(events=events):
                self.fake("events=" + repr(events) + "\nfor event in events: print(json.dumps(event),flush=True)\nsys.exit(" + str(code) + ")\n", complete=False)
                with mock.patch.object(b, "terminate_group", wraps=b.terminate_group) as terminate, contextlib.redirect_stderr(io.StringIO()) as timeline:
                    r = b.exec_codex(self.cfg(), "hi", str(self.cwd), live=True)
                self.assertEqual(r["ok"], expected, r["error"])
                if expected:
                    terminate.assert_not_called()
                    self.assertIn("Reconnecting", timeline.getvalue())
                    self.assertTrue(r["warnings"])
                else:
                    self.assertTrue(r["error"])

    def test_f2_cleanup_pending_blocks_reuse_and_converges(self):
        import signal
        from concurrent.futures import ThreadPoolExecutor
        real_kill, real_killpg = os.kill, os.killpg
        for retry_cancel in (False, True):
            with self.subTest(retry_cancel=retry_cancel):
                self.hanging_fake(detached=True)
                self.ready.unlink(missing_ok=True)
                jid = b.new_run("task", self.cfg(), "hi", str(self.cwd), name="pending")
                def blocked_pid():
                    return int(self.ready.read_text()) if self.ready.exists() else None
                def kill(pid, sig):
                    if pid == blocked_pid() and sig != 0:
                        raise PermissionError("injected descendant kill failure")
                    return real_kill(pid, sig)
                def killpg(pgid, sig):
                    if pgid == blocked_pid() and sig != 0:
                        raise PermissionError("injected descendant group kill failure")
                    return real_killpg(pgid, sig)
                try:
                    with mock.patch.object(b.os, "kill", side_effect=kill), mock.patch.object(b.os, "killpg", side_effect=killpg):
                        r = b.exec_codex(self.cfg(timeout=1), "hi", str(self.cwd), jid=jid)
                    b.finish_run(r)
                    self.assertTrue(r["cleanup_pending"])
                    state = b._refresh(jid)
                    self.assertEqual(state["status"], "cleanup_pending")
                    self.assertTrue(state["timed_out"])
                    self.assertIn("timed out", state["error"])
                    self.assertIn("kill failure", state["cleanup_error"])
                    with self.assertRaisesRegex(SystemExit, "already has active job"):
                        b.prepare_run(self.args(name="pending"), False)
                    if retry_cancel:
                        with contextlib.redirect_stdout(io.StringIO()):
                            b.cmd_cancel(argparse.Namespace(job=jid))
                    else:
                        b.terminate_group(state["pgid"], jid=jid)
                    self.assertEqual(b._refresh(jid)["status"], "cancelled" if retry_cancel else "failed")
                    self.assert_dead(blocked_pid())
                finally:
                    state = b._jread(jid)
                    if state.get("pgid"):
                        b.terminate_group(state["pgid"], jid=jid)

    def test_f3_reused_pid_is_not_adopted_or_signalled(self):
        import signal
        import time
        unrelated = subprocess.Popen([sys.executable, "-c", "import subprocess,sys,time; p=subprocess.Popen([sys.executable,'-c','import time; time.sleep(60)']); print(p.pid,flush=True); time.sleep(60)"], start_new_session=True, stdout=subprocess.PIPE, text=True)
        root = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"], start_new_session=True)
        child = int(unrelated.stdout.readline())
        real_kill, real_killpg = os.kill, os.killpg
        def kill(pid, sig):
            if sig and pid in (unrelated.pid, child):
                self.fail("signalled an unrelated reused PID")
            return real_kill(pid, sig)
        def killpg(pgid, sig):
            if sig and pgid == unrelated.pid:
                self.fail("signalled an unrelated reused process group")
            return real_killpg(pgid, sig)
        try:
            snapshot = b.process_snapshot()
            self.assertIn(root.pid, snapshot)
            identity = snapshot[root.pid][3]
            stale = {unrelated.pid: {"identity": "previous process start time", "pgid": unrelated.pid}}
            tracked = b.collect_descendants(root.pid, stale, snapshot, identity)
            self.assertNotIn(unrelated.pid, tracked)
            self.assertNotIn(child, tracked)
            jid = b.new_run("task", self.cfg(), "hi", str(self.cwd))
            b._jwrite(jid, child_pid=root.pid, pgid=root.pid, child_identity=identity,
                      tracked_pids=[unrelated.pid], tracked_pgids=[unrelated.pid],
                      tracked_processes={str(pid): value for pid, value in stale.items()})
            with mock.patch.object(b.os, "kill", side_effect=kill), mock.patch.object(b.os, "killpg", side_effect=killpg):
                b.terminate_group(root.pid, process=root, jid=jid)
            root.wait(timeout=2)
            st = b._jread(jid)
            self.assertNotIn(str(unrelated.pid), st["tracked_processes"])
            self.assertNotIn(str(child), st["tracked_processes"])
            self.assertIsNone(unrelated.poll())
            # The original CLI PID can itself be recycled; it gets no fallback
            # group signal once a mismatch is observed.
            b._jwrite(jid, child_pid=unrelated.pid, pgid=unrelated.pid,
                      child_identity="previous CLI start time", tracked_processes={})
            with mock.patch.object(b.os, "kill", side_effect=kill), mock.patch.object(b.os, "killpg", side_effect=killpg):
                b.terminate_group(unrelated.pid, jid=jid)
                with mock.patch.object(b, "process_snapshot", return_value=None):
                    b.terminate_group(unrelated.pid, jid=jid)
            self.assertTrue(b._jread(jid)["child_identity_mismatch"])
            self.assertIsNone(unrelated.poll())
        finally:
            for proc in (root, unrelated):
                try:
                    real_killpg(proc.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                proc.wait(timeout=2)
            unrelated.stdout.close()

    def test_f3_identity_checked_again_before_signalling(self):
        import signal
        unrelated = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"], start_new_session=True)
        try:
            current = b.process_snapshot()
            older = dict(current)
            row = current[unrelated.pid]
            older[unrelated.pid] = (row[0], row[1], row[2], "old start")
            jid = b.new_run("task", self.cfg(), "hi", str(self.cwd))
            b._jwrite(jid, child_pid=unrelated.pid, pgid=unrelated.pid, child_identity="old start")
            with mock.patch.object(b, "process_snapshot", side_effect=[older, current, current]), mock.patch.object(b.os, "kill") as kill, mock.patch.object(b.os, "killpg") as killpg:
                b.terminate_group(unrelated.pid, jid=jid)
            kill.assert_not_called()
            killpg.assert_not_called()
            self.assertIsNone(unrelated.poll())
        finally:
            unrelated.kill()
            unrelated.wait(timeout=2)

    def _memory_home(self, *workspaces):
        native = Path(self.env["CODEX_HOME"]) / "memories"
        native.mkdir(parents=True, exist_ok=True)
        (native / "MEMORY.md").write_text("".join(f"# Task Group: x\napplies_to: cwd={w}; reuse_rule=x\n" for w in workspaces))

    def test_memory_scope_binds_to_workspace(self):
        proj = self.root / "proj"; (proj / ".git").mkdir(parents=True)
        wt = proj / ".claude" / "worktrees" / "feature"; wt.mkdir(parents=True)
        sibling = self.root / "proj-sibling"; (sibling / ".git").mkdir(parents=True)
        broad = self.root / "Downloads"; (broad / "new").mkdir(parents=True)
        self._memory_home(proj, sibling, broad, "/Users/x/{a,b}", "relative/path")
        known = b.memory_workspaces()
        self.assertEqual(len(known), 3)  # brace patterns and relative junk are ignored
        mine, others = b.memory_scope(os.path.realpath(wt), known)
        self.assertEqual(mine, [os.path.realpath(proj)])  # worktree inherits its project, not the sibling
        self.assertNotIn(os.path.realpath(proj), others)
        mine, _ = b.memory_scope(os.path.realpath(broad / "new"), known)
        self.assertEqual(mine, [])  # a broad ancestor without .git never matches: new workspace = empty memory
        guard = b.memory_guard(os.path.realpath(wt), known)
        self.assertIn("Use ONLY those entries", guard)
        self.assertIn(os.path.realpath(sibling), guard)
        self.assertIn("new workspace", b.memory_guard(os.path.realpath(broad / "new"), known))

    def test_memory_modes_change_prompt_and_flags(self):
        capture = self.root / "cap.json"
        self.fake("import sys,json\nfrom pathlib import Path\nPath(" + repr(str(capture)) + ").write_text(json.dumps({'argv':sys.argv,'stdin':sys.stdin.read()}))\nprint(json.dumps({'type':'item.completed','item':{'type':'agent_message','text':'ok'}}))\n")
        self._memory_home(self.cwd)
        def run(mode, **kw):
            b.save({**b.load(), "codex_memory": mode})
            b.exec_codex(self.cfg(), kw.pop("prompt", "do it"), str(self.cwd), **kw)
            return json.loads(capture.read_text())
        got = run("scoped")
        self.assertTrue(got["stdin"].startswith("WORKSPACE BINDING"))
        self.assertNotIn("memories.use_memories=false", got["argv"])
        got = run("scoped", prompt=None, review=["--uncommitted"])  # native review cannot take a guard prompt
        self.assertIn("memories.use_memories=false", got["argv"])
        self.assertNotIn("memories.generate_memories=false", got["argv"])
        got = run("off")
        self.assertNotIn("WORKSPACE BINDING", got["stdin"])
        self.assertIn("memories.use_memories=false", got["argv"])
        self.assertIn("memories.generate_memories=false", got["argv"])
        got = run("on")
        self.assertNotIn("WORKSPACE BINDING", got["stdin"])
        self.assertNotIn("memories.use_memories=false", got["argv"])
        with self.assertRaises(SystemExit):
            b.validate("codex_memory", "bogus")

    def test_quota_is_read_from_the_session_rollout(self):
        day = Path(self.env["CODEX_HOME"]) / "sessions" / "2026" / "10" / "04"
        day.mkdir(parents=True)
        limits = {"primary": {"used_percent": 48.0, "window_minutes": 10080, "resets_at": 1791594132}, "secondary": None,
                  "credits": {"has_credits": True, "balance": "62270.44"}}
        event = {"type": "event_msg", "payload": {"type": "token_count", "rate_limits": limits}}
        (day / "rollout-2026-10-04T00-00-00-thread-q.jsonl").write_text("noise\n" + json.dumps(event) + "\n")
        text = b.read_quota("thread-q")
        self.assertIn("48% of weekly used, resets", text)
        self.assertIn("credits 62,270", text)
        self.assertIsNone(b.read_quota("missing"))
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            b.cmd_usage(None)
        self.assertIn("48% of weekly used", out.getvalue())
        self.fake("import json\nprint(json.dumps({'type':'thread.started','thread_id':'thread-q'}))\nprint(json.dumps({'type':'item.completed','item':{'type':'agent_message','text':'ok'}}))\n")
        r = b.exec_codex(self.cfg(), "go", str(self.cwd))
        self.assertIn("quota: 48% of weekly used", r["digest"])


if __name__ == "__main__":
    unittest.main()
