"""Native Windows regressions; stdlib only, no model calls or user configuration."""
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
import threading
import time
import unittest
from unittest import mock

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "codex_bridge.py"
spec = importlib.util.spec_from_file_location("bridge_native_tests", SCRIPT)
b = importlib.util.module_from_spec(spec)
spec.loader.exec_module(b)


@unittest.skipUnless(os.name == "nt", "native Windows APIs")
class WindowsTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name) / "中文 space ' home"
        self.root.mkdir()
        for key, value in {"BRIDGE_HOME": str(self.root), "SETTINGS": str(self.root / "settings.json"),
                           "JOBS": str(self.root / "jobs")}.items():
            patch = mock.patch.object(b, key, value)
            patch.start()
            self.addCleanup(patch.stop)
        self.env = {**os.environ, "CODEX_BRIDGE_HOME": str(self.root), "CODEX_HOME": str(self.root / "native"),
                    "GIT_CEILING_DIRECTORIES": os.path.dirname(os.path.realpath(temporary.name))}
        environment = mock.patch.dict(os.environ, self.env)
        environment.start()
        self.addCleanup(environment.stop)

    def spawn(self, code):
        child = subprocess.Popen([sys.executable, "-X", "utf8", "-u", "-c", code],
                                 stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                 bufsize=0, **b.launch_options())
        def cleanup():
            if child.poll() is None:
                child.kill()
            child.wait(timeout=5)
            child.stdin.close()
            child.stdout.close()
        self.addCleanup(cleanup)
        return child

    def test_stdlib_snapshot_creation_time_and_command_line(self):
        child = self.spawn("import time; time.sleep(30)")
        rows = b.process_snapshot()
        self.assertEqual(rows[child.pid][0], os.getpid())
        self.assertEqual(rows[child.pid][3], b.process_snapshot()[child.pid][3])
        self.assertIn("time.sleep(30)", b.win.command_line(child.pid))

    def test_liveness_and_status_never_use_kill(self):
        child = self.spawn("import time; time.sleep(30)")
        jid = b.new_run("task", b.DEFAULTS, "hi", str(self.root))
        identity = b.process_snapshot()[child.pid][3]
        b._jwrite(jid, pid=child.pid, child_pid=child.pid, child_identity=identity,
                  pgid=child.pid, tracked_processes={str(child.pid): {"identity": identity, "pgid": 0}})
        with mock.patch.object(b.os, "kill", side_effect=AssertionError("os.kill must never run")), \
                mock.patch.object(b, "process_snapshot", return_value=None):
            for _ in range(3):
                self.assertTrue(b._alive(child.pid))
                self.assertEqual(b._refresh(jid)["status"], "running")
        self.assertIsNone(child.poll())

    def test_reused_pid_never_terminated_or_suspended(self):
        child = self.spawn("import time; time.sleep(30)")
        jid = b.new_run("task", b.DEFAULTS, "hi", str(self.root))
        b._jwrite(jid, child_pid=child.pid, pgid=child.pid, child_identity="stale")
        with mock.patch.object(b.win, "signal_verified", wraps=b.win.signal_verified) as signal:
            b.terminate_group(child.pid, jid=jid)
        signal.assert_not_called()
        self.assertTrue(b._jread(jid)["child_identity_mismatch"])
        self.assertIsNone(child.poll())

    def test_identity_checked_on_signalling_handle(self):
        child = self.spawn("import time; time.sleep(30)")
        with mock.patch.object(b.win, "terminate_process", side_effect=AssertionError("wrong identity")), \
                mock.patch.object(b.win, "suspend", side_effect=AssertionError("wrong identity")):
            self.assertFalse(b.win.signal_verified(child.pid, "stale"))
            self.assertFalse(b.win.signal_verified(child.pid, "stale", stop=True))
        self.assertIsNone(child.poll())

    def test_cross_process_file_lock(self):
        counter = self.root / "counter.txt"
        counter.write_text("0", encoding="utf-8")
        code = ("import runpy; from pathlib import Path; b=runpy.run_path(" + repr(str(SCRIPT)) + "); "
                "p=Path(" + repr(str(counter)) + ")\n"
                "for _ in range(15):\n"
                " with b['file_lock'](str(p)+'.lock'):\n"
                "  p.write_text(str(int(p.read_text(encoding='utf-8'))+1),encoding='utf-8')\n")
        children = [subprocess.Popen([sys.executable, "-X", "utf8", "-c", code], env=self.env,
                                     stdout=subprocess.PIPE, stderr=subprocess.PIPE) for _ in range(4)]
        try:
            for child in children:
                _, error = child.communicate(timeout=15)
                self.assertEqual(child.returncode, 0, error.decode("utf-8"))
        finally:
            for child in children:
                if child.poll() is None:
                    child.kill()
                child.wait(timeout=5)
        self.assertEqual(counter.read_text(encoding="utf-8"), "60")

    def test_launcher_without_symlinks_and_utf8_worker(self):
        with mock.patch.object(b.os, "symlink", side_effect=AssertionError("symlink privilege")):
            b.refresh_bridge_link()
        launcher = self.root / "bin" / "codex_bridge.py"
        self.assertFalse(launcher.is_symlink())
        launcher.write_text("stale", encoding="utf-8")
        b.refresh_bridge_link()
        env = {**self.env, "PYTHONUTF8": "0"}
        result = subprocess.run([sys.executable, "-X", "utf8", str(launcher), "config", "set", "profile=中文🚀"],
                                env=env, capture_output=True, encoding="utf-8", timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)["profile"], "中文🚀")
        failed = subprocess.run([sys.executable, "-X", "utf8", str(launcher), "config", "set", "not-a-pair"],
                                env=env, capture_output=True, encoding="utf-8", timeout=10)
        self.assertNotEqual(failed.returncode, 0, "launcher must propagate the bridge's exit code")

    def test_large_input_and_unicode_stream(self):
        child = self.spawn("import sys; text=sys.stdin.read(); print(str(len(text))+':中文🚀')")
        lines = list(b.child_lines(child, "字" * 200000, 10, None))
        self.assertEqual([line.rstrip("\r") for line in lines], ["200000:中文🚀"])
        child.wait(timeout=3)

    def test_partial_output_and_unread_stdin_deadline(self):
        child = self.spawn("import sys,time; sys.stdout.write('partial'); sys.stdout.flush(); time.sleep(30)")
        start = time.monotonic()
        with self.assertRaises(subprocess.TimeoutExpired):
            list(b.child_lines(child, "x" * 1000000, 0.3, None))
        self.assertLess(time.monotonic() - start, 2)
        b.terminate_group(child.pid, process=child)
        child.wait(timeout=3)

    def test_cancellation_has_bounded_pipe_wait(self):
        child = self.spawn("import time; time.sleep(30)")
        with mock.patch.object(b, "_jread", return_value={"status": "cancelled"}), \
                mock.patch.object(b, "CANCEL_GRACE", 0), mock.patch.object(b, "PS_TIMEOUT", 0), \
                mock.patch.object(b, "REAP_TIMEOUT", 0.1):
            with self.assertRaisesRegex(RuntimeError, "cancelled job"):
                list(b.child_lines(child, "", 5, None))

    def test_contained_tree_and_inherited_stdout_cleanup(self):
        # The root exits immediately after spawning: Job membership must retain
        # the child even when a parent-PID scan can no longer discover it.
        ready = self.root / "descendant.txt"
        child = subprocess.Popen([sys.executable, "-X", "utf8", "-u", "-c",
                                  "import subprocess,sys,pathlib; p=subprocess.Popen([sys.executable,'-c','import time; time.sleep(30)']); pathlib.Path(" + repr(str(ready)) + ").write_text(str(p.pid))"],
                                 stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                 bufsize=0, **b.launch_options(suspended=True))
        name = "Local\\codex-bridge-test-" + str(child.pid)
        job = b.win.contain(child, name)
        try:
            child.wait(timeout=5)
            descendant = int(ready.read_text(encoding="utf-8"))
            self.assertIn(descendant, b.win.job_members(name))
            with mock.patch.object(b, "PIPE_DRAIN_TIMEOUT", 0.1):
                with self.assertRaisesRegex(RuntimeError, "descendant kept stdout open"):
                    list(b.child_lines(child, "", 3, None))
            jid = b.new_run("task", b.DEFAULTS, "tree", str(self.root))
            b._jwrite(jid, windows_job=name, child_pid=child.pid, pgid=child.pid)
            with mock.patch.object(b, "process_snapshot", return_value=None):
                b.terminate_group(child.pid, process=child, jid=jid)
            self.assertFalse(b._alive(descendant))
        finally:
            b.win.close(job)
            child.wait(timeout=5)
            child.stdin.close()
            child.stdout.close()

    def test_replace_retries_real_open_destination(self):
        destination = self.root / "state.json"
        source = self.root / "new.json"
        destination.write_text("old", encoding="utf-8")
        source.write_text("new", encoding="utf-8")
        reader = destination.open(encoding="utf-8")
        release = threading.Timer(0.15, reader.close)
        release.start()
        try:
            b.replace_file(source, destination)
        finally:
            reader.close()
            release.join()
        self.assertEqual(destination.read_text(encoding="utf-8"), "new")

    def test_replace_retry_is_bounded_and_unix_path_is_unchanged(self):
        with mock.patch.object(b.os, "replace", side_effect=PermissionError("busy")) as replace, \
                mock.patch.object(b.time, "monotonic", side_effect=[0, 3]):
            with self.assertRaises(PermissionError):
                b.replace_file("a", "b")
        self.assertEqual(replace.call_count, 1)
        with mock.patch.object(b.os, "name", "posix"), \
                mock.patch.object(b.os, "replace", side_effect=PermissionError("busy")) as replace:
            with self.assertRaises(PermissionError):
                b.replace_file("a", "b")
        self.assertEqual(replace.call_count, 1)

    def test_readers_retry_and_preserve_unicode_state(self):
        b.save({**b.DEFAULTS, "profile": "中文🚀"})
        original = open
        with mock.patch.object(b, "open", side_effect=[PermissionError("busy"), PermissionError("busy"), original(b.SETTINGS, encoding="utf-8")], create=True):
            self.assertEqual(b._load_settings()["profile"], "中文🚀")
        jid = b.new_run("task", b.DEFAULTS, "中文🚀", str(self.root))
        b._jwrite(jid, digest="中文🚀")
        self.assertEqual(b._jread(jid)["digest"], "中文🚀")
        self.assertIn("中文🚀", Path(b._job_path(jid, "state.json")).read_text(encoding="utf-8"))

    def test_job_state_retries_while_watch_reader_holds_destination(self):
        jid = b.new_run("task", b.DEFAULTS, "hi", str(self.root))
        reader = open(b._job_path(jid, "state.json"), encoding="utf-8")
        release = threading.Timer(0.15, reader.close)
        release.start()
        try:
            with b.job_state(jid) as state:
                state["digest"] = "watch stays alive"
        finally:
            reader.close()
            release.join()
        self.assertEqual(b._jread(jid)["digest"], "watch stays alive")

    def test_memory_drive_unc_and_multiple_entries(self):
        native = self.root / "native" / "memories"
        native.mkdir(parents=True)
        values = [r"F:\work\project", "C:/space name", r"\\server\share\project"]
        (native / "MEMORY.md").write_text("applies_to: cwd=" + ", ".join(values) + "; reuse_rule=x\n", encoding="utf-8")
        with mock.patch.object(b, "_codex_home", return_value=str(native.parent)):
            self.assertEqual(b.memory_workspaces(), list(map(os.path.realpath, values)))

    def test_extra_args_backslashes_spaces_and_json(self):
        self.assertEqual(b.validate("extra_args", r'--add-dir C:\work\project --add-dir "\\server\share\space dir"'),
                         ["--add-dir", r"C:\work\project", "--add-dir", r"\\server\share\space dir"])
        values = ["--add-dir", r"C:\space dir\project"]
        self.assertEqual(b.validate("extra_args", json.dumps(values)), values)

    def test_windows_activity_classification(self):
        for command, expected in [(r'powershell.exe -Command "Get-Content C:\work\a.txt"', "read"),
                                  (r"pwsh -NoProfile -Command 'Set-Content a.txt hello'", "edit"),
                                  ("python.exe -X utf8 -m unittest", "test"),
                                  ("python -X utf8 -m pytest", "test"),
                                  ("Select-String needle a.txt", "search")]:
            self.assertEqual(b.step_kind(command), expected, command)

    def test_printed_commands_execute_in_powershell(self):
        b.refresh_bridge_link()
        jid = b.new_run("task", b.DEFAULTS, "hi", str(self.root))
        b._jwrite(jid, status="done")
        Path(b._job_path(jid, "result.txt")).write_text("report-ok", encoding="utf-8")
        env = dict(self.env)
        env.pop("CODEX_BRIDGE_HOME")
        command = b.bridge_command("result", jid, "--full")
        result = subprocess.run(["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", command],
                                env=env, capture_output=True, encoding="utf-8", timeout=15)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, "report-ok")

    def test_powershell_native_arguments_keep_literal_quotes(self):
        values = ['model_reasoning_effort="high"', 'a"b c"d', "apostrophe ' dollar $ and & ;", "C:\\space dir\\"]
        command = b.powershell_command([sys.executable, "-X", "utf8", "-c",
                                       "import sys,json;print(json.dumps(sys.argv[1:]))", *values])
        result = subprocess.run(["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", command],
                                capture_output=True, encoding="utf-8", timeout=15)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout), values)

    def test_verbatim_report_bytes_and_worker_utf8(self):
        text = "中文🚀\r\nline\nend\r\n"
        jid = b.new_run("task", b.DEFAULTS, "report", str(self.root))
        b.write_report(b._job_path(jid, "result.txt"), text)
        b._jwrite(jid, status="done")
        result = subprocess.run([sys.executable, str(SCRIPT), "result", jid, "--full"],
                                env={**self.env, "PYTHONUTF8": "0"}, capture_output=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, text.encode("utf-8"))

    def test_failed_cleanup_does_not_block_pipe_close(self):
        child = self.spawn("import time; time.sleep(30)")
        start = time.monotonic()
        with self.assertRaises(subprocess.TimeoutExpired):
            list(b.child_lines(child, "x" * 1000000, 0.15, None))
        child.stdin.close()
        child.stdout.close()
        self.assertLess(time.monotonic() - start, 2)
        self.assertIsNone(child.poll())

    def test_busy_state_is_never_overwritten_as_missing(self):
        jid = b.new_run("task", b.DEFAULTS, "hi", str(self.root))
        with mock.patch.object(b, "read_owned", side_effect=PermissionError("busy")):
            with self.assertRaises(PermissionError):
                with b.job_state(jid):
                    self.fail("unreadable state must not be replaced with an empty object")
        self.assertEqual(b._jread(jid)["status"], "running")

    def test_real_background_worker_uses_utf8_and_finishes(self):
        fake = self.root / "fake codex.py"
        fake.write_text("import sys,json\nif 'debug' in sys.argv:\n print(json.dumps({'models':[{'slug':'gpt-6.1-sol','visibility':'list','supported_reasoning_levels':[{'effort':'medium'}]}]}));sys.exit(0)\nsys.stdin.read()\nprint(json.dumps({'type':'thread.started','thread_id':'native-thread'}))\nprint(json.dumps({'type':'item.completed','item':{'type':'agent_message','text':'中文🚀'}}))\nprint(json.dumps({'type':'turn.completed'}))\n", encoding="utf-8")
        b.save({**b.DEFAULTS, "codex_bin": str(fake), "codex_memory": "off"})
        real_launch = subprocess.Popen
        workers = []
        def capture_launch(*args, **kwargs):
            worker = real_launch(*args, **kwargs)
            workers.append(worker)
            return worker
        with mock.patch.dict(os.environ, {"PYTHONUTF8": "0"}), \
                mock.patch.object(b.subprocess, "Popen", side_effect=capture_launch) as launch, \
                contextlib.redirect_stdout(io.StringIO()):
            b.start_job("task", {**b.DEFAULTS, "preamble": ""}, "中文🚀", str(self.root))
        self.assertEqual(launch.call_args.args[0][1:3], ["-X", "utf8"])
        jid = b.ordered_jobs()[0]
        deadline = time.monotonic() + 10
        state = b._jread(jid)
        try:
            while state["status"] in b.ACTIVE and time.monotonic() < deadline:
                time.sleep(0.05)
                state = b._refresh(jid)
            self.assertEqual(state["status"], "done", state)
            self.assertEqual(b.saved_report(jid), "中文🚀")
        finally:
            if state["status"] in b.ACTIVE:
                b.stop_job(jid)
            deadline = time.monotonic() + 5
            while b._alive(state["pid"]) and time.monotonic() < deadline:
                time.sleep(0.05)
            for worker in workers:
                worker.wait(timeout=5)


if __name__ == "__main__":
    unittest.main()
