#!/usr/bin/env python3
"""Direct gateway supervisor for runtimes where s6 is not PID 1."""

from __future__ import annotations

import json
import os
import signal
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

import yaml

ROOT = Path(os.environ.get("HERMES_HOME", "/opt/data"))
HERMES = os.environ.get("HERMES_BIN", "/opt/hermes/.venv/bin/hermes")
POLL_SECONDS = float(os.environ.get("HERMES_FALLBACK_POLL_SECONDS", "1"))
TRUTHY = {"1", "true", "yes", "on"}
FALSY = {"0", "false", "no", "off"}
STOP = False


@dataclass
class Slot:
    name: str
    home: Path
    process: subprocess.Popen | None = None
    generation: int = 0
    next_start: float = 0.0


def desired(home: Path) -> tuple[bool, int]:
    try:
        state = json.loads((home / "gateway_state.json").read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return False, 0
    if not isinstance(state, dict):
        return False, 0
    value = state.get("desired_state")
    if value is None:
        value = state.get("gateway_state")
        if value in {"draining", "degraded"}:
            value = "running"
    generation = state.get("fallback_restart_generation", 0)
    return value == "running", generation if isinstance(generation, int) else 0


def command(slot: Slot) -> list[str]:
    if slot.name == "default":
        return [HERMES, "gateway", "run", "--replace"]
    return [HERMES, "-p", slot.name, "gateway", "run", "--replace"]


def start(slot: Slot) -> None:
    env = os.environ.copy()
    env.update({
        "HERMES_HOME": str(ROOT),
        "HOME": str(ROOT),
        "HERMES_FALLBACK_SUPERVISED_CHILD": "1",
        "HERMES_S6_SUPERVISED_CHILD": "1",
    })
    slot.process = subprocess.Popen(
        command(slot),
        cwd=ROOT,
        env=env,
        start_new_session=True,
    )
    print(f"[fallback-gateway] started {slot.name} as process group {slot.process.pid}", flush=True)


def signal_group(slot: Slot, sig: signal.Signals) -> None:
    if slot.process is None:
        return
    try:
        os.killpg(slot.process.pid, sig)
    except ProcessLookupError:
        pass


def stop_slots(slots: list[Slot], timeout: float = 3.0) -> None:
    active = [slot for slot in slots if slot.process is not None and slot.process.poll() is None]
    for slot in active:
        signal_group(slot, signal.SIGTERM)
    deadline = time.monotonic() + timeout
    while active and time.monotonic() < deadline:
        active = [slot for slot in active if slot.process is not None and slot.process.poll() is None]
        if active:
            time.sleep(0.05)
    for slot in active:
        signal_group(slot, signal.SIGKILL)
    for slot in slots:
        if slot.process is not None:
            try:
                slot.process.wait(timeout=1)
            except subprocess.TimeoutExpired:
                signal_group(slot, signal.SIGKILL)
                slot.process.wait()
            slot.process = None


def _coerce_bool(value) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        token = value.strip().lower()
        if token in TRUTHY:
            return True
        if token in FALSY:
            return False
        return False
    if isinstance(value, (int, float)):
        return bool(value)
    return False


def multiplex_enabled() -> bool:
    """Match gateway config precedence: recognized env, config, default false."""
    raw = os.environ.get("GATEWAY_MULTIPLEX_PROFILES")
    if raw is not None:
        token = raw.strip().lower()
        if token in TRUTHY:
            return True
        if token in FALSY:
            return False
    config_path = ROOT / "config.yaml"
    if not config_path.is_file():
        return False
    try:
        data = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
        if not isinstance(data, dict):
            raise ValueError("config root is not a mapping")
        value = data.get("multiplex_profiles")
        nested = data.get("gateway")
        if value is None and isinstance(nested, dict):
            value = nested.get("multiplex_profiles")
        return _coerce_bool(value)
    except Exception as exc:
        print(
            f"[fallback-gateway] cannot parse multiplex config; suppressing named gateways: {exc}",
            flush=True,
        )
        return True


def discover() -> dict[str, Path]:
    result = {"default": ROOT}
    if multiplex_enabled():
        return result
    profiles = ROOT / "profiles"
    if profiles.is_dir():
        for path in profiles.iterdir():
            if path.is_dir() and (path / "SOUL.md").is_file():
                result[path.name] = path
    return result


def request_stop(_signum, _frame) -> None:
    global STOP
    STOP = True


def main() -> None:
    signal.signal(signal.SIGTERM, request_stop)
    signal.signal(signal.SIGINT, request_stop)
    signal.signal(signal.SIGHUP, request_stop)
    slots: dict[str, Slot] = {}
    try:
        while not STOP:
            discovered = discover()
            removed = [slots.pop(name) for name in list(slots) if name not in discovered]
            stop_slots(removed)
            for name, home in discovered.items():
                slots.setdefault(name, Slot(name=name, home=home))
            now = time.monotonic()
            for slot in slots.values():
                running, generation = desired(slot.home)
                if slot.process is not None and slot.process.poll() is not None:
                    slot.process.wait()
                    slot.process = None
                    slot.next_start = now + 2
                if slot.process is not None and (not running or generation != slot.generation):
                    stop_slots([slot])
                    slot.next_start = now if running else 0
                slot.generation = generation
                if running and slot.process is None and now >= slot.next_start:
                    start(slot)
            time.sleep(POLL_SECONDS)
    finally:
        stop_slots(list(slots.values()))


if __name__ == "__main__":
    main()
