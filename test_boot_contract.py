import unittest
from pathlib import Path

ROOT = Path(__file__).parent


def derived_dockerfile() -> str:
    copied = ROOT / "railway-derived.Dockerfile"
    return (copied if copied.is_file() else ROOT / "Dockerfile").read_text()


class BootContractTests(unittest.TestCase):
    def test_non_pid_one_fallback_starts_runtime_services(self) -> None:
        entrypoint = (ROOT / "entrypoint-dispatch").read_text()
        secure_start = (ROOT / "secure-start.sh").read_text()
        self.assertIn("sh /etc/cont-init.d/01a-railway-limits", entrypoint)
        self.assertIn("export HERMES_DIRECT_FALLBACK=1", entrypoint)
        self.assertIn("/run/hermes-direct-fallback", entrypoint)
        supervisor = (ROOT / "fallback_gateway_supervisor.py").read_text()
        lifecycle = (ROOT / "fallback_lifecycle.py").read_text()
        for marker in (
            "fallback_start gateway-supervisor",
            "fallback_start dashboard",
            "fallback_start process-reaper",
            "fallback_start filebrowser",
            "fallback_start rclone-webdav",
            "fallback_terminate_group",
            "setsid",
        ):
            self.assertIn(marker, secure_start)
        self.assertIn('{"draining", "degraded"}', supervisor)
        self.assertIn("GATEWAY_MULTIPLEX_PROFILES", supervisor)
        self.assertIn("HERMES_S6_SUPERVISED_CHILD", supervisor)
        self.assertIn("fallback_restart_generation", supervisor)
        self.assertIn("profiles.iterdir()", supervisor)
        self.assertIn("desired_state", lifecycle)

    def test_preflight_and_safe_stage2_patch_precede_root_boot_mutation(self) -> None:
        entrypoint = (ROOT / "entrypoint-dispatch").read_text()
        dockerfile = derived_dockerfile()
        patcher = (ROOT / "patch_stage2_safety.py").read_text()
        preflight = entrypoint.index("railway_prepare.py --ensure-home --validate-only")
        self.assertLess(preflight, entrypoint.index("exec /init"))
        self.assertLess(preflight, entrypoint.index("stage2-hook.sh"))
        self.assertIn("patch_stage2_safety.py", dockerfile)
        self.assertIn("--safe-chown-home", patcher)
        self.assertIn("--safe-chown-subtree", patcher)

    def test_custom_home_moves_base_image_write_targets(self) -> None:
        entrypoint = (ROOT / "entrypoint-dispatch").read_text()
        runtime_patch = (ROOT / "patch_runtime_contract.py").read_text()
        self.assertIn('export HERMES_WRITE_SAFE_ROOT="$HERMES_HOME"', entrypoint)
        self.assertIn(
            'export HERMES_LAZY_INSTALL_TARGET="$HERMES_HOME/lazy-packages"',
            entrypoint,
        )
        self.assertIn('export HERMES_HOME HOME="$HERMES_HOME"', runtime_patch)
        self.assertIn('cd "$HERMES_HOME"', runtime_patch)
        self.assertIn("s6 gateway custom home", runtime_patch)

    def test_image_build_runs_boot_contract_tests(self) -> None:
        dockerfile = derived_dockerfile()
        self.assertIn("COPY Dockerfile /opt/hermes/railway-derived.Dockerfile", dockerfile)
        self.assertIn("rm -rf railway-derived.Dockerfile", dockerfile)
        self.assertIn("COPY test_boot_contract.py", dockerfile)
        self.assertIn(
            "test_boot_contract.py test_fallback_supervision.py &&", dockerfile
        )

    def test_gateway_bootstrap_and_cont_init_order_are_wired(self) -> None:
        dockerfile = derived_dockerfile()
        self.assertIn("HERMES_GATEWAY_BOOTSTRAP_STATE=running", dockerfile)
        self.assertIn("01a-railway-limits", dockerfile)
        self.assertLess("01-hermes-setup", "01a-railway-limits")
        self.assertLess("01a-railway-limits", "02-reconcile-profiles")

    def test_service_scripts_respect_custom_home(self) -> None:
        scripts = [
            ROOT / "dashboard-run",
            ROOT / "s6-rc.d/filebrowser/run",
            ROOT / "s6-rc.d/process-reaper/run",
            ROOT / "s6-rc.d/rclone-webdav/run",
        ]
        for script in scripts:
            text = script.read_text()
            self.assertIn('HERMES_HOME="${HERMES_HOME:-/opt/data}"', text, script)
            self.assertIn("$HERMES_HOME", text, script)


if __name__ == "__main__":
    unittest.main()
