#!/usr/bin/env python3
"""Patch pinned container lifecycle and custom-home contracts."""

import os
from pathlib import Path

ROOT = Path(os.environ.get("HERMES_SOURCE_ROOT", "/opt/hermes"))


def replace_once(path: Path, old: str, new: str, label: str) -> None:
    text = path.read_text()
    count = text.count(old)
    if count != 1:
        raise RuntimeError(f"expected one {label} anchor in {path}, found {count}")
    path.write_text(text.replace(old, new, 1))


def main() -> None:
    wrapper = ROOT / "docker/main-wrapper.sh"
    replace_once(
        wrapper,
        "export HOME=/opt/data",
        'HERMES_HOME="${HERMES_HOME:-/opt/data}"\nexport HERMES_HOME HOME="$HERMES_HOME"',
        "main-wrapper home",
    )
    replace_once(wrapper, "cd /opt/data", 'cd "$HERMES_HOME"', "main-wrapper cwd")

    manager = ROOT / "hermes_cli/service_manager.py"
    replace_once(
        manager,
        '''            "export HOME=/opt/data",\n            "cd /opt/data",''',
        '''            'HERMES_HOME="${HERMES_HOME:-/opt/data}"',\n            'export HERMES_HOME HOME="$HERMES_HOME"',\n            'cd "$HERMES_HOME"',''',
        "s6 gateway custom home",
    )

    for relative in ("bin/hermes", "docker/hermes-exec-shim.sh"):
        path = ROOT / relative
        replace_once(
            path,
            "export HOME=/opt/data",
            'HERMES_HOME="${HERMES_HOME:-/opt/data}"\nexport HERMES_HOME HOME="$HERMES_HOME"',
            f"{relative} home",
        )

    gateway = ROOT / "hermes_cli/gateway.py"
    replace_once(
        gateway,
        '''    from hermes_cli.service_manager import (\n        GatewayNotRegisteredError,''',
        '''    from hermes_cli.fallback_lifecycle import persist_intent\n    if persist_intent(action, get_hermes_home()):\n        return True\n\n    from hermes_cli.service_manager import (\n        GatewayNotRegisteredError,''',
        "fallback single lifecycle",
    )
    replace_once(
        gateway,
        '''    from hermes_cli.service_manager import (\n        detect_service_manager,\n        get_service_manager,\n    )\n\n    if detect_service_manager() != "s6":\n        return False\n    if action not in ("stop", "restart"):''',
        '''    from hermes_constants import get_default_hermes_root\n    from hermes_cli.fallback_lifecycle import persist_all\n    if persist_all(action, get_default_hermes_root()):\n        return True\n\n    from hermes_cli.service_manager import (\n        detect_service_manager,\n        get_service_manager,\n    )\n\n    if detect_service_manager() != "s6":\n        return False\n    if action not in ("stop", "restart"):''',
        "fallback all lifecycle",
    )
    print("patched container fallback lifecycle and custom-home contracts")


if __name__ == "__main__":
    main()
