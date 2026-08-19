#!/usr/bin/env python3
"""Prepare Hermes volume paths without following persisted symlinks."""

from __future__ import annotations

import argparse
import os
import pwd
import secrets
import stat
import sys
from pathlib import Path

DIRECTORIES = ("share", "logs", "runtime")
FILES = ((".filebrowser.db", 0o600),)
# These are the persistent subtrees the pinned image's root stage2 hook may
# recursively inspect or chown. Validate them before /init runs any cont-init
# hook, while no unprivileged service exists to race the check.
ROOT_MANAGED_TREES = (
    "cron",
    "sessions",
    "logs",
    "hooks",
    "memories",
    "skills",
    "skins",
    "plans",
    "workspace",
    "home",
    "profiles",
    "pairing",
    "platforms",
    "lazy-packages",
    ".local",
    "share",
    "runtime",
)
ALLOWED_CHOWN_SUBTREES = frozenset((*ROOT_MANAGED_TREES, "platforms/pairing", "logs/gateways"))
ROOT_MANAGED_FILES = frozenset(
    {
        "auth.json",
        "auth.lock",
        ".env",
        "state.db",
        "state.db-shm",
        "state.db-wal",
        "hermes_state.db",
        "response_store.db",
        "response_store.db-shm",
        "response_store.db-wal",
        "gateway.pid",
        "gateway.lock",
        "gateway_state.json",
        "processes.json",
        "active_profile",
        "config.yaml",
        ".filebrowser.db",
    }
)


def ensure_absolute_directory(path: Path) -> None:
    if not path.is_absolute() or any(part in {".", ".."} for part in path.parts):
        raise RuntimeError(f"HERMES_HOME must be a normalized absolute path: {path}")
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | os.O_NOFOLLOW
    current_fd = os.open("/", flags)
    try:
        for component in path.parts[1:]:
            try:
                os.mkdir(component, 0o755, dir_fd=current_fd)
            except FileExistsError:
                pass
            child_fd = os.open(component, flags, dir_fd=current_fd)
            os.close(current_fd)
            current_fd = child_fd
    finally:
        os.close(current_fd)


def open_absolute_directory(path: Path) -> int:
    """Open every path component with O_NOFOLLOW and return the final fd."""
    if not path.is_absolute() or ".." in path.parts:
        raise RuntimeError(f"HERMES_HOME must be an absolute normalized path: {path}")
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | os.O_NOFOLLOW
    fd = os.open("/", flags)
    try:
        for component in path.parts[1:]:
            next_fd = os.open(component, flags, dir_fd=fd)
            os.close(fd)
            fd = next_fd
        return fd
    except Exception:
        os.close(fd)
        raise


def open_relative_directory(parent_fd: int, relative: str) -> int:
    """Open a normalized relative directory path without following any component."""
    path = Path(relative)
    if path.is_absolute() or not path.parts or any(part in {".", ".."} for part in path.parts):
        raise RuntimeError(f"directory target must be normalized and relative: {relative}")
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | os.O_NOFOLLOW
    fd = os.dup(parent_fd)
    try:
        for component in path.parts:
            next_fd = os.open(component, flags, dir_fd=fd)
            os.close(fd)
            fd = next_fd
        return fd
    except Exception:
        os.close(fd)
        raise


def validate_tree_fd(
    directory_fd: int,
    display_path: str,
    links: dict[tuple[int, int], list[int]],
) -> None:
    """Inventory regular-file links and reject unsafe special files."""
    path_flags = os.O_PATH | os.O_CLOEXEC | os.O_NOFOLLOW
    directory_flags = os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | os.O_NOFOLLOW
    for name in os.listdir(directory_fd):
        path_fd = os.open(name, path_flags, dir_fd=directory_fd)
        try:
            info = os.fstat(path_fd)
            child_display = f"{display_path}/{name}"
            if stat.S_ISLNK(info.st_mode):
                # Persisted managed trees may intentionally contain links.
                # Descriptor-safe ownership repair skips them entirely.
                continue
            if stat.S_ISREG(info.st_mode):
                key = (info.st_dev, info.st_ino)
                record = links.setdefault(key, [info.st_nlink, 0])
                if record[0] != info.st_nlink:
                    raise RuntimeError(f"unstable link count: {child_display}")
                record[1] += 1
                continue
            if not stat.S_ISDIR(info.st_mode):
                raise RuntimeError(f"refusing special file: {child_display}")
            child_fd = os.open(name, directory_flags, dir_fd=directory_fd)
            try:
                validate_tree_fd(child_fd, child_display, links)
            finally:
                os.close(child_fd)
        finally:
            os.close(path_fd)


def validate_root_managed_paths(home: Path) -> set[tuple[int, int]]:
    """Validate every subtree the upstream root boot hook may mutate."""
    links: dict[tuple[int, int], list[int]] = {}
    home_fd = open_absolute_directory(home)
    try:
        directory_flags = os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | os.O_NOFOLLOW
        for name in ROOT_MANAGED_TREES:
            try:
                fd = os.open(name, directory_flags, dir_fd=home_fd)
            except FileNotFoundError:
                continue
            try:
                validate_tree_fd(fd, f"{home}/{name}", links)
            finally:
                os.close(fd)
        for name in ROOT_MANAGED_FILES:
            try:
                file_fd = os.open(name, os.O_PATH | os.O_CLOEXEC | os.O_NOFOLLOW, dir_fd=home_fd)
            except FileNotFoundError:
                continue
            try:
                info = os.fstat(file_fd)
                if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                    raise RuntimeError(f"refusing unsafe root-managed file: {home}/{name}")
            finally:
                os.close(file_fd)
    finally:
        os.close(home_fd)
    # Hard-linked regular files are inventoried but deliberately excluded from
    # root ownership repair. Their other links may live in an unmanaged tree.
    return set(links)


def chown_tree_fd(
    directory_fd: int,
    display_path: str,
    uid: int,
    gid: int,
) -> None:
    """Descriptor-safe recursive ownership repair that never follows links."""
    directory_flags = os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | os.O_NOFOLLOW
    file_flags = os.O_RDONLY | os.O_NONBLOCK | os.O_CLOEXEC | os.O_NOFOLLOW
    for name in os.listdir(directory_fd):
        info = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
        child_display = f"{display_path}/{name}"
        if stat.S_ISLNK(info.st_mode):
            continue
        if stat.S_ISREG(info.st_mode):
            child_fd = os.open(name, file_flags, dir_fd=directory_fd)
            try:
                opened = os.fstat(child_fd)
                if not stat.S_ISREG(opened.st_mode):
                    raise RuntimeError(f"file type changed during ownership repair: {child_display}")
                if opened.st_nlink != 1:
                    # Ownership is inode-wide. Never mutate a multiply linked
                    # file because another name may sit outside this subtree.
                    continue
                os.fchown(child_fd, uid, gid)
            finally:
                os.close(child_fd)
            continue
        if not stat.S_ISDIR(info.st_mode):
            raise RuntimeError(f"refusing special file: {child_display}")
        child_fd = os.open(name, directory_flags, dir_fd=directory_fd)
        try:
            chown_tree_fd(child_fd, child_display, uid, gid)
            os.fchown(child_fd, uid, gid)
        finally:
            os.close(child_fd)


def safe_chown(home: Path, relative: str | None, runtime_user: str) -> None:
    account = pwd.getpwnam(runtime_user)
    validate_root_managed_paths(home)
    home_fd = open_absolute_directory(home)
    try:
        if relative is None:
            os.fchown(home_fd, account.pw_uid, account.pw_gid)
            return
        if relative not in ALLOWED_CHOWN_SUBTREES:
            raise RuntimeError(f"refusing unmanaged ownership target: {relative}")
        target_fd = open_relative_directory(home_fd, relative)
        try:
            chown_tree_fd(
                target_fd,
                f"{home}/{relative}",
                account.pw_uid,
                account.pw_gid,
            )
            os.fchown(target_fd, account.pw_uid, account.pw_gid)
        finally:
            os.close(target_fd)
    finally:
        os.close(home_fd)


def safe_chown_directory(home: Path, relative: str, runtime_user: str) -> None:
    if relative not in ALLOWED_CHOWN_SUBTREES:
        raise RuntimeError(f"refusing unmanaged directory target: {relative}")
    account = pwd.getpwnam(runtime_user)
    validate_root_managed_paths(home)
    home_fd = open_absolute_directory(home)
    try:
        target_fd = open_relative_directory(home_fd, relative)
        try:
            os.fchown(target_fd, account.pw_uid, account.pw_gid)
        finally:
            os.close(target_fd)
    finally:
        os.close(home_fd)


def safe_fix_file(home: Path, relative: str, runtime_user: str, mode: int | None) -> None:
    if relative not in ROOT_MANAGED_FILES:
        raise RuntimeError(f"refusing unmanaged file target: {relative}")
    account = pwd.getpwnam(runtime_user)
    validate_root_managed_paths(home)
    home_fd = open_absolute_directory(home)
    try:
        target_fd = os.open(
            relative,
            os.O_RDONLY | os.O_NONBLOCK | os.O_CLOEXEC | os.O_NOFOLLOW,
            dir_fd=home_fd,
        )
        try:
            info = os.fstat(target_fd)
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                raise RuntimeError(f"refusing unsafe file target: {home}/{relative}")
            os.fchown(target_fd, account.pw_uid, account.pw_gid)
            if mode is not None:
                os.fchmod(target_fd, mode)
        finally:
            os.close(target_fd)
    finally:
        os.close(home_fd)


def _unlink_opened_name(parent_fd: int, name: str, opened_fd: int) -> None:
    """Unlink name only while it still denotes the inode held by opened_fd."""
    expected = os.fstat(opened_fd)
    actual = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
    if (actual.st_dev, actual.st_ino) != (expected.st_dev, expected.st_ino):
        raise RuntimeError(f"temporary bootstrap path was replaced: {name}")
    os.unlink(name, dir_fd=parent_fd)


def safe_create_file(
    home: Path,
    relative: str,
    runtime_user: str,
    mode: int,
    content: bytes,
) -> None:
    """Failure-atomically install an allowlisted top-level file once."""
    if relative not in ROOT_MANAGED_FILES or Path(relative).name != relative:
        raise RuntimeError(f"refusing unmanaged file target: {relative}")
    account = pwd.getpwnam(runtime_user)
    validate_root_managed_paths(home)
    home_fd = open_absolute_directory(home)
    temp_name = f".railway-bootstrap-{os.getpid()}-{secrets.token_hex(16)}"
    fd = -1
    temp_exists = False
    try:
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC | os.O_NOFOLLOW
        fd = os.open(temp_name, flags, 0o600, dir_fd=home_fd)
        temp_exists = True
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise RuntimeError(f"refusing unsafe bootstrap inode for: {home}/{relative}")
        view = memoryview(content)
        while view:
            written = os.write(fd, view)
            if written <= 0:
                raise RuntimeError(f"short write creating {home}/{relative}")
            view = view[written:]
        os.fchmod(fd, mode)
        os.fchown(fd, account.pw_uid, account.pw_gid)
        os.fsync(fd)
        os.link(
            temp_name,
            relative,
            src_dir_fd=home_fd,
            dst_dir_fd=home_fd,
            follow_symlinks=False,
        )
        _unlink_opened_name(home_fd, temp_name, fd)
        temp_exists = False
        os.fsync(home_fd)
        installed = os.fstat(fd)
        if installed.st_nlink != 1:
            raise RuntimeError(f"bootstrap link count is unsafe: {home}/{relative}")
    finally:
        if fd >= 0 and temp_exists:
            try:
                _unlink_opened_name(home_fd, temp_name, fd)
                os.fsync(home_fd)
            except FileNotFoundError:
                pass
        if fd >= 0:
            os.close(fd)
        os.close(home_fd)


def ensure_directory(home_fd: int, name: str, uid: int, gid: int) -> None:
    try:
        os.mkdir(name, 0o755, dir_fd=home_fd)
    except FileExistsError:
        pass
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | os.O_NOFOLLOW
    fd = os.open(name, flags, dir_fd=home_fd)
    try:
        info = os.fstat(fd)
        if not stat.S_ISDIR(info.st_mode):
            raise RuntimeError(f"{name} is not a directory")
        os.fchown(fd, uid, gid)
    finally:
        os.close(fd)


def ensure_regular_file(home_fd: int, name: str, mode: int, uid: int, gid: int) -> None:
    flags = os.O_RDWR | os.O_NONBLOCK | os.O_CLOEXEC | os.O_NOFOLLOW | os.O_CREAT
    fd = os.open(name, flags, mode, dir_fd=home_fd)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise RuntimeError(f"{name} must be a singly linked regular file")
        os.fchmod(fd, mode)
        os.fchown(fd, uid, gid)
    finally:
        os.close(fd)


def prepare(home: Path, runtime_user: str = "hermes") -> None:
    user = pwd.getpwnam(runtime_user)
    home_fd = open_absolute_directory(home)
    try:
        for name in DIRECTORIES:
            ensure_directory(home_fd, name, user.pw_uid, user.pw_gid)
        for name, mode in FILES:
            ensure_regular_file(home_fd, name, mode, user.pw_uid, user.pw_gid)
    finally:
        os.close(home_fd)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--ensure-home", action="store_true")
    parser.add_argument("--validate-only", action="store_true")
    parser.add_argument("--safe-chown-home", action="store_true")
    parser.add_argument("--safe-chown-subtree")
    parser.add_argument("--safe-chown-directory")
    parser.add_argument("--safe-fix-file")
    parser.add_argument("--safe-create-file")
    parser.add_argument("--mode", type=lambda value: int(value, 8))
    args = parser.parse_args()
    home = Path(os.environ.get("HERMES_HOME", "/opt/data"))
    if args.ensure_home:
        ensure_absolute_directory(home)
    if args.safe_chown_home:
        safe_chown(home, None, "hermes")
        return
    if args.safe_chown_subtree is not None:
        safe_chown(home, args.safe_chown_subtree, "hermes")
        return
    if args.safe_chown_directory is not None:
        safe_chown_directory(home, args.safe_chown_directory, "hermes")
        return
    if args.safe_fix_file is not None:
        safe_fix_file(home, args.safe_fix_file, "hermes", args.mode)
        return
    if args.safe_create_file is not None:
        if args.mode is None:
            raise RuntimeError("--safe-create-file requires --mode")
        safe_create_file(home, args.safe_create_file, "hermes", args.mode, sys.stdin.buffer.read())
        return
    if args.validate_only:
        validate_root_managed_paths(home)
        print("[railway-prepare] validated root-managed persistent paths")
        return
    prepare(home, "hermes")
    print("[railway-prepare] validated and prepared persistent paths")


if __name__ == "__main__":
    main()
