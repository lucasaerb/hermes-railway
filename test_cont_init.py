import os
import pwd
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import railway_prepare


class ContInitTests(unittest.TestCase):
    def test_fresh_volume_permissions_and_limits_before_reconcile(self) -> None:
        user = pwd.getpwnam("hermes")
        root = Path(tempfile.mkdtemp())
        try:
            os.chown(root, user.pw_uid, user.pw_gid)
            fake = root / "fake-hermes"
            fake.write_text(
                "#!/bin/sh\n"
                "printf '%s\\n' \"$*\" >> \"$HERMES_HOME/config-calls.log\"\n"
                "touch \"$HERMES_HOME/config.yaml\"\n"
            )
            fake.chmod(fake.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
            fake_s6 = root / "fake-s6-setuidgid"
            fake_s6.write_text("#!/bin/sh\nshift\nexec \"$@\"\n")
            fake_s6.chmod(fake_s6.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)

            s6_runner = "/command/s6-setuidgid" if os.getuid() == 0 else str(fake_s6)
            script = Path(__file__).with_name("railway-cont-init").read_text()
            env = {
                **os.environ,
                "HERMES_HOME": str(root),
                "HERMES_BIN": str(fake),
                "HERMES_PREPARE_PYTHON": sys.executable,
                "HERMES_PREPARE_SCRIPT": str(Path(__file__).with_name("railway_prepare.py")),
                "S6_SETUIDGID": s6_runner,
            }
            subprocess.run(["sh"], input=script, text=True, env=env, check=True)

            calls = (root / "config-calls.log").read_text().splitlines()
            self.assertEqual(
                calls,
                [
                    "config set max_concurrent_sessions 2",
                    "config set delegation.max_concurrent_children 2",
                    "config set cron.max_parallel_jobs 1",
                    "config set kanban.max_spawn 1",
                    "config set kanban.max_in_progress 1",
                    "config set kanban.auto_decompose_per_tick 1",
                ],
            )
            for path in (
                root / "share",
                root / "logs",
                root / "runtime",
                root / ".filebrowser.db",
                root / "config.yaml",
                root / "config-calls.log",
            ):
                self.assertEqual(path.stat().st_uid, user.pw_uid, path)

            order = sorted(
                [
                    "01-hermes-setup",
                    "015-supervise-perms",
                    "01a-railway-limits",
                    "02-reconcile-profiles",
                ]
            )
            self.assertEqual(
                order,
                [
                    "01-hermes-setup",
                    "015-supervise-perms",
                    "01a-railway-limits",
                    "02-reconcile-profiles",
                ],
            )
        finally:
            shutil.rmtree(root)

    def test_symlinked_volume_operands_fail_closed(self) -> None:
        runtime_user = pwd.getpwuid(os.getuid()).pw_name
        for name in (*railway_prepare.DIRECTORIES, ".filebrowser.db"):
            with self.subTest(name=name), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp) / "home"
                root.mkdir()
                if name == ".filebrowser.db":
                    victim = Path(tmp) / "victim-file"
                    victim.write_text("do not touch")
                else:
                    victim = Path(tmp) / "victim-dir"
                    victim.mkdir()
                before = victim.stat()
                (root / name).symlink_to(victim, target_is_directory=victim.is_dir())
                with self.assertRaises(OSError):
                    railway_prepare.prepare(root, runtime_user)
                after = victim.stat()
                self.assertEqual(
                    (after.st_uid, after.st_gid, after.st_mode),
                    (before.st_uid, before.st_gid, before.st_mode),
                )

    def test_symlinked_home_component_fails_closed(self) -> None:
        runtime_user = pwd.getpwuid(os.getuid()).pw_name
        with tempfile.TemporaryDirectory() as tmp:
            real_home = Path(tmp) / "real-home"
            real_home.mkdir()
            alias = Path(tmp) / "alias"
            alias.symlink_to(real_home, target_is_directory=True)
            with self.assertRaises(OSError):
                railway_prepare.prepare(alias, runtime_user)
            self.assertEqual(list(real_home.iterdir()), [])

    def test_directory_collision_fails_closed(self) -> None:
        runtime_user = pwd.getpwuid(os.getuid()).pw_name
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "share").write_text("not a directory")
            with self.assertRaises(OSError):
                railway_prepare.prepare(root, runtime_user)
            self.assertEqual((root / "share").read_text(), "not a directory")

    def test_path_replacement_after_open_does_not_touch_replacement(self) -> None:
        runtime_user = pwd.getpwuid(os.getuid()).pw_name
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "home"
            root.mkdir()
            (root / "share").mkdir()
            victim = Path(tmp) / "victim"
            victim.mkdir()
            before = victim.stat()
            real_fstat = railway_prepare.os.fstat
            swapped = False

            def swap_then_stat(fd: int) -> os.stat_result:
                nonlocal swapped
                if not swapped:
                    swapped = True
                    (root / "share").rename(root / "opened-share")
                    (root / "share").symlink_to(victim, target_is_directory=True)
                return real_fstat(fd)

            with mock.patch.object(railway_prepare.os, "fstat", side_effect=swap_then_stat):
                railway_prepare.prepare(root, runtime_user)
            after = victim.stat()
            self.assertEqual(
                (after.st_uid, after.st_gid, after.st_mode),
                (before.st_uid, before.st_gid, before.st_mode),
            )
            self.assertTrue((root / "share").is_symlink())
            self.assertTrue((root / "opened-share").is_dir())

    def test_preflight_rejects_unsafe_top_level_state_files(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "home"
            root.mkdir()
            victim = Path(tmp) / "victim"
            victim.write_text("do not touch")
            (root / ".env").symlink_to(victim)
            with self.assertRaises(RuntimeError):
                railway_prepare.validate_root_managed_paths(root)
            (root / ".env").unlink()
            os.link(victim, root / "config.yaml")
            with self.assertRaises(RuntimeError):
                railway_prepare.validate_root_managed_paths(root)

    def test_recursive_chown_rejects_replaced_intermediate_component(self) -> None:
        runtime_user = pwd.getpwuid(os.getuid()).pw_name
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "home"
            root.mkdir()
            external = Path(tmp) / "external"
            (external / "pairing").mkdir(parents=True)
            (root / "platforms").symlink_to(external, target_is_directory=True)
            with mock.patch.object(railway_prepare, "validate_root_managed_paths"):
                with self.assertRaises(OSError):
                    railway_prepare.safe_chown(root, "platforms/pairing", runtime_user)

    def test_safe_create_file_is_nofollow_and_create_once(self) -> None:
        runtime_user = pwd.getpwuid(os.getuid()).pw_name
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "home"
            root.mkdir()
            victim = Path(tmp) / "victim"
            victim.write_bytes(b"protected")
            (root / "auth.json").symlink_to(victim)
            with self.assertRaises(RuntimeError):
                railway_prepare.safe_create_file(root, "auth.json", runtime_user, 0o600, b"new")
            self.assertEqual(victim.read_bytes(), b"protected")
            (root / "auth.json").unlink()
            railway_prepare.safe_create_file(root, "auth.json", runtime_user, 0o600, b"new")
            self.assertEqual((root / "auth.json").read_bytes(), b"new")
            self.assertEqual(stat.S_IMODE((root / "auth.json").stat().st_mode), 0o600)
            with self.assertRaises(FileExistsError):
                railway_prepare.safe_create_file(root, "auth.json", runtime_user, 0o600, b"again")

    def test_safe_create_file_failure_leaves_no_initialized_path(self) -> None:
        runtime_user = pwd.getpwuid(os.getuid()).pw_name
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "home"
            root.mkdir()
            with mock.patch.object(railway_prepare.os, "write", side_effect=OSError("ENOSPC")):
                with self.assertRaises(OSError):
                    railway_prepare.safe_create_file(root, "auth.json", runtime_user, 0o600, b"new")
            self.assertFalse((root / "auth.json").exists())
            self.assertEqual(list(root.glob(".railway-bootstrap-*")), [])

    def test_safe_recursive_chown_skips_symlinks_and_hardlinks(self) -> None:
        runtime_user = pwd.getpwuid(os.getuid()).pw_name
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "home"
            (root / "profiles/example").mkdir(parents=True)
            external = Path(tmp) / "external"
            external.write_text("do not touch")
            hardlink = root / "profiles/example/hardlink"
            os.link(external, hardlink)
            target = Path(tmp) / "target"
            target.write_text("do not touch")
            (root / "profiles/example/link").symlink_to(target)
            protected_inodes = {
                (external.stat().st_dev, external.stat().st_ino),
                (target.stat().st_dev, target.stat().st_ino),
            }
            changed: list[tuple[int, int]] = []

            def record_chown(fd: int, _uid: int, _gid: int) -> None:
                info = os.fstat(fd)
                changed.append((info.st_dev, info.st_ino))

            with mock.patch.object(railway_prepare.os, "fchown", side_effect=record_chown):
                railway_prepare.safe_chown(root, "profiles", runtime_user)
            self.assertTrue(protected_inodes.isdisjoint(changed))

    def test_safe_home_creation_rejects_symlink_component(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / "target"
            target.mkdir()
            (root / "linked").symlink_to(target, target_is_directory=True)
            with self.assertRaises(OSError):
                railway_prepare.ensure_absolute_directory(root / "linked/home")
            self.assertFalse((target / "home").exists())

    def test_hardlinked_database_fails_closed(self) -> None:
        runtime_user = pwd.getpwuid(os.getuid()).pw_name
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "home"
            root.mkdir()
            victim = Path(tmp) / "victim"
            victim.write_text("do not touch")
            os.link(victim, root / ".filebrowser.db")
            before = victim.stat()
            with self.assertRaises(RuntimeError):
                railway_prepare.prepare(root, runtime_user)
            after = victim.stat()
            self.assertEqual(
                (after.st_uid, after.st_gid, after.st_mode),
                (before.st_uid, before.st_gid, before.st_mode),
            )


if __name__ == "__main__":
    unittest.main()
