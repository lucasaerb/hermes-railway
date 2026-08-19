#!/usr/bin/env python3
"""Apply the Railway PID-pressure load-shedding hooks to pinned Hermes."""

import os
from pathlib import Path


def replace_once(path: Path, old: str, new: str) -> None:
    text = path.read_text()
    count = text.count(old)
    if count != 1:
        raise SystemExit(f"expected one patch anchor in {path}, found {count}")
    path.write_text(text.replace(old, new, 1))


def main() -> None:
    root = Path(os.getenv("HERMES_SOURCE_ROOT", "/opt/hermes"))
    replace_once(
        root / "tools/delegate_tool.py",
        '''    if parent_agent is None:\n        return tool_error("delegate_task requires a parent agent context.")\n\n    # Operator-controlled kill switch''',
        '''    if parent_agent is None:\n        return tool_error("delegate_task requires a parent agent context.")\n\n    # Railway/cgroup load shedding: reject new fan-out before the host reaches\n    # its PID/thread ceiling. Existing children continue and the stale-process\n    # reaper frees old preview/test/browser groups in parallel.\n    from tools.pid_pressure import effective_shed_threshold, pid_pressure, should_shed\n    if should_shed():\n        current, maximum, threshold = pid_pressure()\n        threshold = effective_shed_threshold(maximum, threshold)\n        return tool_error(\n            f"Delegation temporarily paused by PID pressure "\n            f"({current}/{maximum} slots; threshold {threshold})."\n        )\n\n    # Operator-controlled kill switch''',
    )
    replace_once(
        root / "gateway/kanban_watchers.py",
        '''            conn = None\n            fingerprint = _board_db_fingerprint(slug)''',
        '''            # Do not claim a new worker when cgroup PID/thread pressure\n            # has crossed the Railway load-shed threshold. The task stays ready\n            # and the next dispatcher tick retries automatically.\n            from tools.pid_pressure import effective_shed_threshold, pid_pressure, should_shed\n            if should_shed():\n                current, maximum, threshold = pid_pressure()\n                threshold = effective_shed_threshold(maximum, threshold)\n                logger.warning(\n                    "kanban dispatcher: PID pressure %s/%s crossed threshold %s; "\n                    "skipping new worker spawn",\n                    current, maximum, threshold,\n                )\n                return None\n\n            conn = None\n            fingerprint = _board_db_fingerprint(slug)''',
    )
    print("patched Hermes delegation and Kanban PID-pressure guards")


if __name__ == "__main__":
    main()
