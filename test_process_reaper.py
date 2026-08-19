import dataclasses
import importlib.util
import os
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

MODULE_PATH = Path(__file__).with_name("process_reaper.py")
SPEC = importlib.util.spec_from_file_location("process_reaper", MODULE_PATH)
assert SPEC and SPEC.loader
reaper = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = reaper
SPEC.loader.exec_module(reaper)


class ProcessReaperTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tempdir = tempfile.TemporaryDirectory()
        self._original_log_path = getattr(reaper, "LOG_PATH")
        self._original_state_path = getattr(reaper, "STATE_PATH")
        setattr(reaper, "LOG_PATH", Path(self._tempdir.name) / "reaper.log")
        setattr(reaper, "STATE_PATH", Path(self._tempdir.name) / "state.json")

    def tearDown(self) -> None:
        setattr(reaper, "LOG_PATH", self._original_log_path)
        setattr(reaper, "STATE_PATH", self._original_state_path)
        self._tempdir.cleanup()

    def test_exact_ephemeral_command_shapes(self) -> None:
        true_cases = [
            ("next-server", "1000"),
            ("next-server (v15.5.7)", "1000"),
            ("/tmp/agent-browser-linux", "daemon"),
            ("npm", "run", "start"),
            ("npm", "run", "test"),
            ("python3", "-m", "http.server", "8000"),
            ("node", "--test", "suite.js"),
            ("next", "dev"),
            ("node", "/app/next", "start"),
            ("vite", "preview"),
        ]
        false_cases = [
            ("bash", "-c", "echo next-server"),
            ("python3", "script.py", "npm run start"),
            ("python3", "app.py", "-m", "http.server"),
            ("python-helper", "-m", "http.server"),
            ("agent-browser-linux", "--help", "daemon"),
            ("node", "app.js", "--test"),
            ("npm", "run", "build", "start"),
            ("node", "app.js", "/tmp/next-server.log"),
            ("my-next-server-wrapper", "start"),
        ]
        for argv in true_cases:
            self.assertTrue(reaper.is_known_ephemeral(argv), argv)
        for argv in false_cases:
            self.assertFalse(reaper.is_known_ephemeral(argv), argv)

    def test_active_task_takes_precedence_over_ended_session(self) -> None:
        owner = ("task", "/tmp/board.db\0task-1")
        identity = reaper.ProcessIdentity(
            pid=999_991,
            pgid=999_991,
            start_ticks=1,
            owner=owner,
            argv=("next-server", "1000"),
        )
        env = {
            "HERMES_KANBAN_TASK": "task-1",
            "HERMES_KANBAN_BOARD": "/tmp/board.db",
            "HERMES_SESSION_ID": "ended-session",
        }
        with (
            mock.patch.object(reaper, "all_processes", return_value=[(identity, env)]),
            mock.patch.object(reaper, "process_age", return_value=120),
            mock.patch.object(reaper, "task_is_finished", return_value=False),
            mock.patch.object(reaper, "session_is_finished", return_value=True),
        ):
            self.assertEqual(reaper.discover_candidates(False), [])

    def test_unowned_group_member_blocks_candidate(self) -> None:
        owner = ("session", "finished-session")
        ephemeral = reaper.ProcessIdentity(
            pid=999_981,
            pgid=999_981,
            start_ticks=1,
            owner=owner,
            argv=("next-server", "1000"),
        )
        unowned = reaper.ProcessIdentity(
            pid=999_982,
            pgid=999_981,
            start_ticks=2,
            owner=None,
            argv=("sleep", "1000"),
        )
        records = [
            (ephemeral, {"HERMES_SESSION_ID": "finished-session"}),
            (unowned, {}),
        ]
        with (
            mock.patch.object(reaper, "all_processes", return_value=records),
            mock.patch.object(reaper, "process_age", return_value=120),
            mock.patch.object(reaper, "session_is_finished", return_value=True),
        ):
            self.assertEqual(reaper.discover_candidates(False), [])

    def test_identity_change_fails_revalidation(self) -> None:
        env = os.environ.copy()
        env["HERMES_SESSION_ID"] = "reaper-identity-test"
        process = subprocess.Popen(
            ["bash", "-c", 'exec -a next-server sleep 1000'],
            env=env,
            start_new_session=True,
        )
        try:
            time.sleep(0.1)
            identity = reaper.read_identity(process.pid)
            self.assertIsNotNone(identity)
            assert identity is not None
            changed = dataclasses.replace(identity, start_ticks=identity.start_ticks + 1)
            candidate = reaper.Candidate(
                pgid=identity.pgid,
                owner=identity.owner,
                env={"HERMES_SESSION_ID": "reaper-identity-test"},
                members=(changed,),
                ephemeral=(changed,),
                age_seconds=999,
                reason="test",
            )
            self.assertIsNone(reaper.revalidate(candidate))
            self.assertIsNone(process.poll())
        finally:
            if process.poll() is None:
                os.killpg(os.getpgid(process.pid), signal.SIGKILL)
            process.wait(timeout=5)

    def test_pressure_thresholds_scale_below_configured_ceiling(self) -> None:
        self.assertEqual(reaper.effective_pressure_thresholds(500), (375, 425))
        self.assertEqual(reaper.effective_pressure_thresholds(1000), (750, 850))

    def test_delayed_kill_uses_original_pidfd(self) -> None:
        env = os.environ.copy()
        env["HERMES_SESSION_ID"] = "reaper-kill-test"
        process = subprocess.Popen(
            ["bash", "-c", 'trap "" TERM; exec -a next-server sleep 1000'],
            env=env,
            start_new_session=True,
        )
        try:
            time.sleep(0.1)
            identity = reaper.read_identity(process.pid)
            self.assertIsNotNone(identity)
            assert identity is not None and identity.owner is not None
            candidate = reaper.Candidate(
                pgid=identity.pgid,
                owner=identity.owner,
                env={"HERMES_SESSION_ID": "reaper-kill-test"},
                members=(identity,),
                ephemeral=(identity,),
                age_seconds=999,
                reason="test",
            )
            reaper.reap(candidate)
            self.assertEqual(process.wait(timeout=5), -signal.SIGKILL)
        finally:
            if process.poll() is None:
                os.kill(process.pid, signal.SIGKILL)
                process.wait(timeout=5)

    def test_late_same_owner_member_gets_pidfd_before_reap_completes(self) -> None:
        owner = ("session", "late-member")
        first = reaper.ProcessIdentity(900001, 900001, 1, ("next-server", "1000"), owner)
        late = reaper.ProcessIdentity(900002, 900001, 2, ("sleep", "1000"), owner)
        candidate = reaper.Candidate(
            pgid=900001,
            owner=owner,
            env={"HERMES_SESSION_ID": "late-member"},
            members=(first,),
            ephemeral=(first,),
            age_seconds=999,
            reason="test",
        )
        signaled = []
        with (
            mock.patch.object(reaper, "revalidate", return_value=(first,)),
            mock.patch.object(
                reaper,
                "open_validated_pidfds",
                side_effect=[[(first, 101)], [(late, 102)]],
            ),
            mock.patch.object(reaper, "snapshot_group", side_effect=[(first, late), ()]),
            mock.patch.object(
                reaper,
                "signal_handles",
                side_effect=lambda handles, sig: signaled.append(([item.pid for item, _fd in handles], sig)),
            ),
            mock.patch.object(reaper.os, "close"),
        ):
            reaper.reap(candidate)
        term_pids = {pid for pids, sig in signaled if sig == signal.SIGTERM for pid in pids}
        self.assertEqual(term_pids, {first.pid, late.pid})

    def test_leaderless_group_is_discovered_and_reaped_with_pidfd(self) -> None:
        env = os.environ.copy()
        env["HERMES_SESSION_ID"] = "reaper-leaderless-test"
        with tempfile.TemporaryDirectory() as tmp:
            pid_file = Path(tmp) / "child.pid"
            code = (
                "import os,sys; "
                "child=os.fork(); "
                "(open(sys.argv[1],'w').write(str(child)), os._exit(0)) if child else "
                "os.execvpe('/bin/sleep',['next-server','1000'],os.environ)"
            )
            leader = subprocess.Popen(
                ["python3", "-c", code, str(pid_file)],
                env=env,
                start_new_session=True,
            )
            leader.wait(timeout=5)
            deadline = time.monotonic() + 5
            while not pid_file.exists() and time.monotonic() < deadline:
                time.sleep(0.05)
            child_pid = int(pid_file.read_text())
            try:
                time.sleep(0.1)
                pgid = os.getpgid(child_pid)
                self.assertEqual(pgid, leader.pid)
                self.assertFalse(Path(f"/proc/{pgid}").exists())
                with mock.patch.object(reaper, "session_is_finished", return_value=True):
                    matches = [
                        item
                        for item in reaper.discover_candidates(False)
                        if item.pgid == pgid
                    ]
                self.assertEqual(len(matches), 1)
                reaper.reap(matches[0])
                deadline = time.monotonic() + 5
                while Path(f"/proc/{child_pid}").exists() and time.monotonic() < deadline:
                    time.sleep(0.05)
                self.assertFalse(Path(f"/proc/{child_pid}").exists())
            finally:
                try:
                    os.kill(child_pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass


if __name__ == "__main__":
    unittest.main()
