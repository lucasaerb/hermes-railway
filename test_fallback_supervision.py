import json
import os
import shutil
import subprocess
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

import fallback_lifecycle

ROOT = Path(__file__).parent


class FallbackSupervisionTests(unittest.TestCase):
    def wait_for(self, predicate, timeout=5):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if predicate():
                return
            time.sleep(0.05)
        self.fail("timed out waiting for fallback supervisor state")

    def write_state(self, home: Path, state: dict) -> None:
        home.mkdir(parents=True, exist_ok=True)
        (home / "gateway_state.json").write_text(json.dumps(state))

    def test_dynamic_intent_profiles_restart_custom_home_and_shutdown(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            home = base / "custom-home"
            log = base / "gateway.log"
            fake = base / "hermes"
            fake.write_text(
                "#!/bin/sh\n"
                f"printf 'START:%s:HOME=%s:PWD=%s\\n' \"$*\" \"$HOME\" \"$PWD\" >> {log}\n"
                f"trap 'printf \"TERM:%s\\n\" \"$*\" >> {log}; exit 0' TERM INT HUP\n"
                "while :; do sleep 0.1; done\n"
            )
            fake.chmod(0o755)
            self.write_state(home, {"desired_state": "stopped"})
            env = os.environ.copy()
            env.update(
                {
                    "HERMES_HOME": str(home),
                    "HOME": str(home),
                    "HERMES_BIN": str(fake),
                    "HERMES_DIRECT_FALLBACK": "1",
                    "HERMES_FALLBACK_POLL_SECONDS": "0.05",
                }
            )
            supervisor = subprocess.Popen(
                ["/opt/hermes/.venv/bin/python", str(ROOT / "fallback_gateway_supervisor.py")],
                env=env,
            )
            try:
                time.sleep(0.2)
                self.assertFalse(log.exists())
                with mock.patch.dict(os.environ, env, clear=False):
                    self.assertTrue(fallback_lifecycle.persist_intent("start", home))
                self.wait_for(lambda: log.exists() and "START:gateway run --replace" in log.read_text())
                first_count = log.read_text().count("START:gateway run --replace")
                with mock.patch.dict(os.environ, env, clear=False):
                    fallback_lifecycle.persist_intent("restart", home)
                self.wait_for(lambda: log.read_text().count("START:gateway run --replace") > first_count)

                profile = home / "profiles" / "new-profile"
                profile.mkdir(parents=True)
                (profile / "SOUL.md").write_text("# Profile\n")
                self.write_state(profile, {"gateway_state": "degraded"})
                self.wait_for(lambda: "START:-p new-profile gateway run --replace" in log.read_text())

                with mock.patch.dict(os.environ, env, clear=False):
                    fallback_lifecycle.persist_intent("stop", home)
                self.wait_for(lambda: log.read_text().count("TERM:gateway run --replace") >= 1)
                shutil.rmtree(profile)
                self.wait_for(lambda: "TERM:-p new-profile gateway run --replace" in log.read_text())

                text = log.read_text()
                self.assertIn(f"HOME={home}:PWD={home}", text)
            finally:
                supervisor.terminate()
                supervisor.wait(timeout=5)

    def test_config_multiplex_suppresses_dynamic_named_profiles(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            home = base / "home"
            profile = home / "profiles" / "named"
            profile.mkdir(parents=True)
            (profile / "SOUL.md").write_text("# Profile\n")
            self.write_state(home, {"desired_state": "running"})
            self.write_state(profile, {"desired_state": "running"})
            (home / "config.yaml").write_text("gateway:\n  multiplex_profiles: true\n")
            log = base / "log"
            fake = base / "hermes"
            fake.write_text(
                "#!/bin/sh\n"
                f"printf '%s\\n' \"$*\" >> {log}\n"
                "trap 'exit 0' TERM INT HUP\n"
                "while :; do sleep 0.1; done\n"
            )
            fake.chmod(0o755)
            env = os.environ.copy()
            env.update(
                {
                    "HERMES_HOME": str(home),
                    "HOME": str(home),
                    "HERMES_BIN": str(fake),
                    "HERMES_FALLBACK_POLL_SECONDS": "0.05",
                    "GATEWAY_MULTIPLEX_PROFILES": "   ",
                }
            )
            supervisor = subprocess.Popen(
                ["/opt/hermes/.venv/bin/python", str(ROOT / "fallback_gateway_supervisor.py")], env=env
            )
            try:
                self.wait_for(log.exists)
                time.sleep(0.2)
                text = log.read_text()
                self.assertIn("gateway run --replace", text)
                self.assertNotIn("-p named", text)
            finally:
                supervisor.terminate()
                supervisor.wait(timeout=5)
    def test_multiplex_env_override_precedes_config(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            (home / "config.yaml").write_text("gateway:\n  multiplex_profiles: true\n")
            env = os.environ.copy()
            env.update({"HERMES_HOME": str(home), "GATEWAY_MULTIPLEX_PROFILES": "off"})
            output = subprocess.check_output(
                [
                    "/opt/hermes/.venv/bin/python",
                    "-c",
                    "import fallback_gateway_supervisor as f; print(int(f.multiplex_enabled()))",
                ],
                cwd=ROOT,
                env=env,
                text=True,
            )
            self.assertEqual(output.strip(), "0")


if __name__ == "__main__":
    unittest.main()
