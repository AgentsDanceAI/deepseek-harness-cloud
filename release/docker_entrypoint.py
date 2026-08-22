#!/usr/bin/env python3
"""Migrate legacy data-volume ownership, then run the app unprivileged."""

from __future__ import annotations

import os
import stat
import sys
from collections.abc import MutableMapping
from pathlib import Path
from typing import Callable


APP_UID = 10001
APP_GID = 10001
DATA_DIR = Path("/app/data")
AUTH_SECRET_MAX_BYTES = 4096


def prepare_auth_secret(environment: MutableMapping[str, str] = os.environ) -> bool:
    """Read a bind-mounted secret while privileged, then pass only its value.

    Package installers deliberately create the host file with mode 0600. Its
    host UID is not necessarily the container app UID, so the root entrypoint
    must read it before dropping privileges. Never follow a link or echo the
    value in an error.
    """
    if environment.get("AUTH_SECRET", "").strip():
        return False
    source = environment.get("AUTH_SECRET_FILE", "")
    if not source:
        return False
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(source, flags)
        try:
            metadata = os.fstat(descriptor)
            if not stat.S_ISREG(metadata.st_mode) or metadata.st_size > AUTH_SECRET_MAX_BYTES:
                raise RuntimeError("auth secret must be a small regular file")
            value = os.read(descriptor, AUTH_SECRET_MAX_BYTES + 1)
        finally:
            os.close(descriptor)
    except OSError as error:
        raise RuntimeError("auth secret file is not safely readable") from error
    if len(value) > AUTH_SECRET_MAX_BYTES:
        raise RuntimeError("auth secret file is too large")
    try:
        secret = value.decode("utf-8").strip()
    except UnicodeDecodeError as error:
        raise RuntimeError("auth secret file must be UTF-8") from error
    if not secret:
        raise RuntimeError("auth secret file is empty")
    environment["AUTH_SECRET"] = secret
    environment.pop("AUTH_SECRET_FILE", None)
    return True


def migrate_data_dir(
    data_dir: Path = DATA_DIR,
    *,
    uid: int = APP_UID,
    gid: int = APP_GID,
    lchown: Callable[[str | bytes | os.PathLike[str] | os.PathLike[bytes], int, int], None] = os.lchown,
) -> bool:
    """Chown one legacy volume without following links or crossing mounts.

    Old releases ran as root, so existing SQLite files are root-owned. The
    migration runs before the server starts. Once the volume root is owned by
    the app UID, later starts are O(1).
    """
    root_stat = os.lstat(data_dir)
    if stat.S_ISLNK(root_stat.st_mode):
        raise RuntimeError(f"data directory must not be a symlink: {data_dir}")
    if not stat.S_ISDIR(root_stat.st_mode):
        raise RuntimeError(f"data path must be a directory: {data_dir}")
    if root_stat.st_uid == uid and root_stat.st_gid == gid:
        return False

    root_device = root_stat.st_dev
    for current, directories, files in os.walk(data_dir, topdown=True, followlinks=False):
        current_path = Path(current)
        safe_directories: list[str] = []
        for name in directories:
            path = current_path / name
            entry = os.lstat(path)
            lchown(path, uid, gid)
            if not stat.S_ISLNK(entry.st_mode) and entry.st_dev == root_device:
                safe_directories.append(name)
        directories[:] = safe_directories
        for name in files:
            lchown(current_path / name, uid, gid)
        if current_path != data_dir:
            lchown(current_path, uid, gid)
    # The root ownership is the crash-safe completion marker checked above.
    # It must remain untouched until every descendant has been migrated.
    lchown(data_dir, uid, gid)
    return True


def main(argv: list[str] | None = None) -> int:
    command = list(sys.argv[1:] if argv is None else argv)
    if not command:
        raise RuntimeError("entrypoint requires a command")
    if os.geteuid() == 0:
        prepare_auth_secret()
        migrate_data_dir()
        os.setgroups([])
        os.setgid(APP_GID)
        os.setuid(APP_UID)
    elif os.geteuid() != APP_UID:
        raise RuntimeError(f"entrypoint must start as root or uid {APP_UID}")
    os.execvp(command[0], command)
    return 127


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, RuntimeError) as error:
        print(f"dsh-cloud entrypoint: {error}", file=sys.stderr)
        raise SystemExit(1) from error
