"""Persist gateway lifecycle intent for non-s6 container supervision."""

from __future__ import annotations

import json
import os
import secrets
from pathlib import Path


def _write_atomic(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.parent / f".{path.name}.{os.getpid()}.{secrets.token_hex(8)}.tmp"
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC | os.O_NOFOLLOW
    fd = os.open(temp, flags, 0o600)
    try:
        data = (json.dumps(payload, sort_keys=True) + "\n").encode()
        view = memoryview(data)
        while view:
            written = os.write(fd, view)
            if written <= 0:
                raise OSError("short gateway-state write")
            view = view[written:]
        os.fsync(fd)
    finally:
        os.close(fd)
    try:
        os.replace(temp, path)
        directory_fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        try:
            temp.unlink()
        except FileNotFoundError:
            pass


def fallback_active() -> bool:
    return os.environ.get("HERMES_DIRECT_FALLBACK") == "1" or Path(
        "/run/hermes-direct-fallback"
    ).is_file()


def persist_intent(action: str, home: Path) -> bool:
    """Handle start, stop, or restart when direct fallback supervision is active."""
    if not fallback_active():
        return False
    if action not in {"start", "stop", "restart"}:
        return False
    state_path = home / "gateway_state.json"
    state: dict = {}
    try:
        loaded = json.loads(state_path.read_text(encoding="utf-8"))
        if isinstance(loaded, dict):
            state = loaded
    except (OSError, ValueError, TypeError):
        pass
    state["desired_state"] = "stopped" if action == "stop" else "running"
    if action == "restart":
        generation = state.get("fallback_restart_generation", 0)
        state["fallback_restart_generation"] = generation + 1 if isinstance(generation, int) else 1
    _write_atomic(state_path, state)
    print(f"✓ Gateway {action} intent persisted for direct container supervision")
    return True


def persist_all(action: str, root: Path) -> bool:
    if not fallback_active() or action not in {"stop", "restart"}:
        return False
    homes = [root]
    profiles = root / "profiles"
    if profiles.is_dir():
        homes.extend(path for path in profiles.iterdir() if path.is_dir() and (path / "SOUL.md").is_file())
    for home in homes:
        persist_intent(action, home)
    return True
