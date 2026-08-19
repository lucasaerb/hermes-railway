#!/usr/bin/env python3
"""Safely reap stale Hermes-owned preview, test, and browser processes.

Railway limits this container to 1,000 PID/thread slots. Tool subprocesses
inherit HERMES_SESSION_ID and Kanban workers also inherit HERMES_KANBAN_*.
This watchdog only targets exact, known ephemeral command shapes. It snapshots
every member of the owning process group, revalidates identities, then signals
through Linux pidfds so PID or process-group reuse cannot hit a new process.
"""

from __future__ import annotations

import json
import os
import re
import select
import signal
import sqlite3
import time
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any

POLL_SECONDS = int(os.getenv("HERMES_REAPER_POLL_SECONDS", "300"))
MAX_AGE_SECONDS = int(os.getenv("HERMES_REAPER_MAX_AGE_SECONDS", "21600"))
PRESSURE_REAP_AGE_SECONDS = int(os.getenv("HERMES_REAPER_PRESSURE_AGE_SECONDS", "1800"))
SHED_THRESHOLD = int(os.getenv("HERMES_PID_SHED_THRESHOLD", "750"))
ALERT_THRESHOLD = int(os.getenv("HERMES_PID_ALERT_THRESHOLD", "850"))
HERMES_HOME = Path(os.getenv("HERMES_HOME", "/opt/data"))
LOG_PATH = HERMES_HOME / "logs/process-reaper.log"
STATE_PATH = HERMES_HOME / "runtime/pid-guard-state.json"
CLOCK_TICKS = os.sysconf("SC_CLK_TCK")
NEXT_SERVER_RE = re.compile(r"^next-server(?: \(v[0-9.]+\))?$")
PYTHON_RE = re.compile(r"^python(?:[0-9]+(?:\.[0-9]+)*)?$")


@dataclass(frozen=True)
class ProcessIdentity:
    pid: int
    pgid: int
    start_ticks: int
    argv: tuple[str, ...]
    owner: tuple[str, str] | None


@dataclass(frozen=True)
class Candidate:
    pgid: int
    owner: tuple[str, str]
    env: dict[str, str]
    members: tuple[ProcessIdentity, ...]
    ephemeral: tuple[ProcessIdentity, ...]
    age_seconds: int
    reason: str


def log(event: str, **fields: object) -> None:
    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    row = {"ts": int(time.time()), "event": event, **fields}
    with LOG_PATH.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(row, sort_keys=True) + "\n")


def cgroup_pids() -> tuple[int, int]:
    current = int(Path("/sys/fs/cgroup/pids.current").read_text().strip())
    raw_max = Path("/sys/fs/cgroup/pids.max").read_text().strip()
    maximum = int(raw_max) if raw_max != "max" else 2**31 - 1
    return current, maximum


def proc_env(pid: int) -> dict[str, str]:
    try:
        raw = Path(f"/proc/{pid}/environ").read_bytes()
    except (FileNotFoundError, PermissionError, ProcessLookupError):
        return {}
    wanted = {
        "HERMES_SESSION_ID",
        "HERMES_KANBAN_TASK",
        "HERMES_KANBAN_DB",
        "HERMES_KANBAN_BOARD",
    }
    env: dict[str, str] = {}
    for item in raw.split(bytes([0])):
        if b"=" not in item:
            continue
        key, value = item.split(b"=", 1)
        name = key.decode(errors="replace")
        if name in wanted:
            env[name] = value.decode(errors="replace")
    return env


def owner_key(env: dict[str, str]) -> tuple[str, str] | None:
    task_id = env.get("HERMES_KANBAN_TASK", "")
    if task_id:
        return ("task", f"{env.get('HERMES_KANBAN_DB', '')}\0{task_id}")
    session_id = env.get("HERMES_SESSION_ID", "")
    if session_id:
        return ("session", session_id)
    return None


def proc_argv(pid: int) -> tuple[str, ...]:
    raw = Path(f"/proc/{pid}/cmdline").read_bytes()
    return tuple(
        part.decode(errors="replace")
        for part in raw.split(bytes([0]))
        if part
    )


def proc_start_ticks(pid: int) -> int:
    raw = Path(f"/proc/{pid}/stat").read_text()
    close = raw.rfind(")")
    if close < 0:
        raise ValueError("malformed proc stat")
    tail = raw[close + 2 :].split()
    return int(tail[19])


def read_identity(pid: int) -> ProcessIdentity | None:
    try:
        argv = proc_argv(pid)
        if not argv:
            return None
        env = proc_env(pid)
        owner = owner_key(env)
        return ProcessIdentity(
            pid=pid,
            pgid=os.getpgid(pid),
            start_ticks=proc_start_ticks(pid),
            argv=argv,
            owner=owner,
        )
    except (FileNotFoundError, ProcessLookupError, PermissionError, ValueError, OSError):
        return None


def process_age(identity: ProcessIdentity) -> float:
    uptime = float(Path("/proc/uptime").read_text().split()[0])
    return max(0.0, uptime - identity.start_ticks / CLOCK_TICKS)


def basename(value: str) -> str:
    return Path(value).name


def is_known_ephemeral(argv: tuple[str, ...]) -> bool:
    """Match exact executable/token shapes, never arbitrary substrings."""
    if not argv:
        return False
    first = basename(argv[0])

    if first == "agent-browser-linux" and argv[1:2] == ("daemon",):
        return True
    if NEXT_SERVER_RE.fullmatch(first):
        return True
    if first in {"npm", "npm-cli.js"}:
        npm_args = argv[1:]
        if npm_args[:2] in {
            ("run", "dev"),
            ("run", "start"),
            ("run", "test"),
        } or npm_args[:1] in {("start",), ("test",)}:
            return True
    if PYTHON_RE.fullmatch(first) and argv[1:3] == ("-m", "http.server"):
        return True
    if first == "node" and argv[1:2] == ("--test",):
        return True
    if first == "next" and argv[1:2] in {("dev",), ("start",)}:
        return True
    if len(argv) >= 3 and first == "node" and basename(argv[1]) == "next" and argv[2] in {"dev", "start"}:
        return True
    if first == "vite" and argv[1:2] in {("dev",), ("preview",)}:
        return True
    if len(argv) >= 3 and first == "node" and basename(argv[1]) == "vite" and argv[2] in {"dev", "preview"}:
        return True
    return False


def is_protected(argv: tuple[str, ...]) -> bool:
    if not argv:
        return True
    names = {basename(item) for item in argv[:2]}
    if names & {"s6-supervise", "s6-svscan", "caddy", "process_reaper.py"}:
        return True
    if "hermes" in names and any(token in argv for token in ("gateway", "dashboard")):
        return True
    return False


def all_processes() -> list[tuple[ProcessIdentity, dict[str, str]]]:
    records: list[tuple[ProcessIdentity, dict[str, str]]] = []
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit():
            continue
        pid = int(entry.name)
        identity = read_identity(pid)
        if identity is None:
            continue
        records.append((identity, proc_env(pid)))
    return records


def task_is_finished(env: dict[str, str]) -> bool:
    task_id = env.get("HERMES_KANBAN_TASK")
    db = env.get("HERMES_KANBAN_DB")
    if not task_id or not db or not Path(db).is_file():
        return False
    try:
        with sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=2) as conn:
            row = conn.execute("SELECT status FROM tasks WHERE id = ?", (task_id,)).fetchone()
        return bool(row and row[0] != "running")
    except sqlite3.Error:
        return False


def session_is_finished(session_id: str) -> bool:
    if not session_id:
        return False
    candidates = [HERMES_HOME / "state.db", *(HERMES_HOME / "profiles").glob("*/state.db")]
    for db in candidates:
        if not db.is_file():
            continue
        try:
            with sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=2) as conn:
                row = conn.execute(
                    "SELECT ended_at, expiry_finalized FROM sessions WHERE id = ?",
                    (session_id,),
                ).fetchone()
            if row:
                return row[0] is not None or bool(row[1])
        except sqlite3.Error:
            continue
    return False


def discover_candidates(under_pressure: bool) -> list[Candidate]:
    groups: dict[int, list[tuple[ProcessIdentity, dict[str, str]]]] = {}
    for identity, env in all_processes():
        groups.setdefault(identity.pgid, []).append((identity, env))

    result: list[Candidate] = []
    own_group = os.getpgrp()
    age_limit = PRESSURE_REAP_AGE_SECONDS if under_pressure else MAX_AGE_SECONDS
    for pgid, records in groups.items():
        if pgid in {1, own_group}:
            continue
        members = tuple(item[0] for item in records)
        ephemeral = tuple(
            item
            for item in members
            if item.owner is not None and is_known_ephemeral(item.argv)
        )
        if not ephemeral:
            continue
        owner = ephemeral[0].owner
        if owner is None:
            continue
        if any(item.owner != owner or is_protected(item.argv) for item in members):
            log("candidate_skipped", pgid=pgid, reason="mixed_or_protected_group")
            continue
        env = next(item[1] for item in records if item[0] == ephemeral[0])
        age = int(max(process_age(item) for item in ephemeral))
        if owner[0] == "task":
            finished = task_is_finished(env)
        else:
            finished = session_is_finished(env.get("HERMES_SESSION_ID", ""))
        if not finished and age < age_limit:
            continue
        result.append(
            Candidate(
                pgid=pgid,
                owner=owner,
                env=env,
                members=members,
                ephemeral=ephemeral,
                age_seconds=age,
                reason="owner_finished" if finished else "age_limit",
            )
        )
    return result


def snapshot_group(pgid: int) -> tuple[ProcessIdentity, ...] | None:
    members: list[ProcessIdentity] = []
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit():
            continue
        pid = int(entry.name)
        try:
            if os.getpgid(pid) != pgid:
                continue
        except (ProcessLookupError, PermissionError, OSError):
            continue
        identity = read_identity(pid)
        if identity is None:
            log("reap_skipped", pgid=pgid, pid=pid, reason="group_member_unreadable")
            return None
        members.append(identity)
    return tuple(members)


def revalidate(candidate: Candidate) -> tuple[ProcessIdentity, ...] | None:
    current = snapshot_group(candidate.pgid)
    if not current:
        return None
    expected = set(candidate.members)
    if any(item not in expected for item in current):
        log("reap_skipped", pgid=candidate.pgid, reason="group_membership_changed")
        return None
    if any(item.owner != candidate.owner or is_protected(item.argv) for item in current):
        log("reap_skipped", pgid=candidate.pgid, reason="identity_or_owner_changed")
        return None
    if not any(item in set(candidate.ephemeral) for item in current):
        log("reap_skipped", pgid=candidate.pgid, reason="ephemeral_anchor_gone")
        return None
    return current


def open_validated_pidfds(members: tuple[ProcessIdentity, ...]) -> list[tuple[ProcessIdentity, int]]:
    handles: list[tuple[ProcessIdentity, int]] = []
    try:
        for expected in members:
            fd = os.pidfd_open(expected.pid, 0)
            actual = read_identity(expected.pid)
            if actual != expected:
                os.close(fd)
                raise RuntimeError(f"identity changed for pid {expected.pid}")
            handles.append((expected, fd))
        return handles
    except (AttributeError, OSError, RuntimeError) as exc:
        for _identity, fd in handles:
            os.close(fd)
        raise RuntimeError(str(exc)) from exc


def refresh_group_handles(
    candidate: Candidate,
    handles: list[tuple[ProcessIdentity, int]],
    *,
    require_ephemeral: bool,
) -> tuple[tuple[ProcessIdentity, ...], list[tuple[ProcessIdentity, int]]] | None:
    """Rescan a PGID and acquire pidfds for every newly observed safe member."""
    current = snapshot_group(candidate.pgid)
    if current is None:
        return None
    if any(item.owner != candidate.owner or is_protected(item.argv) for item in current):
        log("reap_skipped", pgid=candidate.pgid, reason="late_mixed_or_protected_member")
        return None
    if require_ephemeral and not any(
        item.owner == candidate.owner and is_known_ephemeral(item.argv) for item in current
    ):
        log("reap_skipped", pgid=candidate.pgid, reason="ephemeral_anchor_gone")
        return None
    known = {identity for identity, _fd in handles}
    additions = tuple(item for item in current if item not in known)
    try:
        new_handles = open_validated_pidfds(additions) if additions else []
    except RuntimeError as exc:
        log("reap_skipped", pgid=candidate.pgid, reason="late_pidfd_validation_failed", error=str(exc))
        return None
    handles.extend(new_handles)
    return current, new_handles


def signal_handles(handles: list[tuple[ProcessIdentity, int]], sig: signal.Signals) -> None:
    sender = getattr(signal, "pidfd_send_signal", None)
    if sender is None:
        raise RuntimeError("pidfd_send_signal is unavailable")
    for _identity, fd in handles:
        try:
            sender(fd, sig)
        except ProcessLookupError:
            pass


def reap(candidate: Candidate) -> None:
    members = revalidate(candidate)
    if members is None:
        return
    try:
        handles = open_validated_pidfds(members)
    except RuntimeError as exc:
        log("reap_skipped", pgid=candidate.pgid, reason="pidfd_validation_failed", error=str(exc))
        return

    completed = False
    try:
        refreshed = refresh_group_handles(candidate, handles, require_ephemeral=True)
        if refreshed is None:
            return
        signal_handles(handles, signal.SIGTERM)
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            refreshed = refresh_group_handles(candidate, handles, require_ephemeral=False)
            if refreshed is None:
                log("reap_incomplete", pgid=candidate.pgid, reason="unsafe_late_group_change")
                return
            current, new_handles = refreshed
            if new_handles:
                signal_handles(new_handles, signal.SIGTERM)
            if not current:
                completed = True
                break
            time.sleep(0.05)

        if not completed:
            poller = select.poll()
            pending: set[int] = set()
            for _identity, fd in handles:
                poller.register(fd, select.POLLIN)
                pending.add(fd)
            for fd, _event in poller.poll(0):
                pending.discard(fd)
            if pending:
                signal_handles(
                    [(identity, fd) for identity, fd in handles if fd in pending],
                    signal.SIGKILL,
                )
            empty_scans = 0
            final_deadline = time.monotonic() + 1
            while time.monotonic() < final_deadline and empty_scans < 3:
                refreshed = refresh_group_handles(candidate, handles, require_ephemeral=False)
                if refreshed is None:
                    log("reap_incomplete", pgid=candidate.pgid, reason="unsafe_post_kill_group_change")
                    return
                current, new_handles = refreshed
                if new_handles:
                    signal_handles(new_handles, signal.SIGKILL)
                    empty_scans = 0
                elif current:
                    empty_scans = 0
                else:
                    empty_scans += 1
                time.sleep(0.05)
            completed = empty_scans >= 3
        if not completed:
            log("reap_incomplete", pgid=candidate.pgid, reason="surviving_group_members")
            return
    except (OSError, RuntimeError) as exc:
        log("reap_error", pgid=candidate.pgid, error=str(exc))
        return
    finally:
        for _identity, fd in handles:
            os.close(fd)

    log(
        "reaped",
        pgid=candidate.pgid,
        pids=[item.pid for item in members],
        age_seconds=candidate.age_seconds,
        reason=candidate.reason,
        commands=[list(item.argv) for item in candidate.ephemeral],
    )


def env_value(name: str) -> str:
    value = os.getenv(name, "").strip()
    if value:
        return value
    env_file = HERMES_HOME / ".env"
    if not env_file.is_file():
        return ""
    try:
        for raw in env_file.read_text(errors="replace").splitlines():
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, candidate = line.split("=", 1)
            if key.strip() == name:
                return candidate.strip().strip('"').strip("'")
    except OSError:
        return ""
    return ""


def send_pressure_alert(message: str) -> None:
    token = env_value("TELEGRAM_BOT_TOKEN")
    chat_id = env_value("TELEGRAM_HOME_CHANNEL")
    if not token or not chat_id:
        log("alert_skipped", reason="telegram_not_configured")
        return
    data = urllib.parse.urlencode({"chat_id": chat_id, "text": message}).encode()
    request = urllib.request.Request(
        f"https://api.telegram.org/bot{token}/sendMessage",
        data=data,
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            response.read()
        log("alert_sent")
    except Exception as exc:
        log("alert_error", error=f"{type(exc).__name__}: {exc}")


def effective_pressure_thresholds(maximum: int) -> tuple[int, int]:
    shed = min(SHED_THRESHOLD, max(1, maximum * 3 // 4))
    alert = min(ALERT_THRESHOLD, max(shed, maximum * 17 // 20))
    return shed, alert


def record_pressure(current: int, maximum: int) -> None:
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    previous = "normal"
    if STATE_PATH.is_file():
        try:
            previous = json.loads(STATE_PATH.read_text()).get("level", "normal")
        except (OSError, ValueError, TypeError):
            pass
    shed_threshold, alert_threshold = effective_pressure_thresholds(maximum)
    level = "alert" if current >= alert_threshold else "shed" if current >= shed_threshold else "normal"
    if level != previous:
        log("pid_pressure", level=level, previous=previous, current=current, maximum=maximum)
        if level == "alert":
            send_pressure_alert(
                f"Hermes PID pressure alert: {current}/{maximum} slots. "
                "New fan-out is paused and stale-process cleanup is running."
            )
        elif previous == "alert" and level != "alert":
            send_pressure_alert(f"Hermes PID pressure recovered: {current}/{maximum} slots.")
    STATE_PATH.write_text(json.dumps({"level": level, "current": current, "maximum": maximum}))


def run_once() -> None:
    current, maximum = cgroup_pids()
    record_pressure(current, maximum)
    shed_threshold, _alert_threshold = effective_pressure_thresholds(maximum)
    for candidate in discover_candidates(under_pressure=current >= shed_threshold):
        reap(candidate)


def main() -> None:
    log(
        "started",
        poll_seconds=POLL_SECONDS,
        max_age_seconds=MAX_AGE_SECONDS,
        shed_threshold=SHED_THRESHOLD,
        alert_threshold=ALERT_THRESHOLD,
    )
    while True:
        try:
            run_once()
        except Exception as exc:
            log("loop_error", error=f"{type(exc).__name__}: {exc}")
        time.sleep(POLL_SECONDS)


if __name__ == "__main__":
    main()
