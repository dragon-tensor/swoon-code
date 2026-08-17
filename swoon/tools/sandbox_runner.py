"""Trusted in-sandbox launcher for disposable command workspaces.

This module is executed inside Bubblewrap. It is intentionally standalone: it
copies the interpreter-filtered output seed into the size-limited tmpfs and
then replaces itself with the requested argv without invoking a shell.
"""

from __future__ import annotations

import os
import stat
import sys
from pathlib import Path


_READY_MARKER = b"\x1eSWOON_SANDBOX_READY\x1e\n"


def _copy_tree(source: Path, destination: Path) -> None:
    for entry in sorted(os.scandir(source), key=lambda item: item.name):
        source_path = Path(entry.path)
        destination_path = destination / entry.name
        item_stat = entry.stat(follow_symlinks=False)
        if stat.S_ISLNK(item_stat.st_mode):
            raise RuntimeError("seed contains a symbolic link")
        if stat.S_ISDIR(item_stat.st_mode):
            destination_path.mkdir(mode=0o700)
            _copy_tree(source_path, destination_path)
            continue
        if not stat.S_ISREG(item_stat.st_mode):
            raise RuntimeError("seed contains a special file")

        source_flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
        source_flags |= getattr(os, "O_NOFOLLOW", 0)
        destination_flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        destination_flags |= getattr(os, "O_CLOEXEC", 0)
        source_fd = os.open(source_path, source_flags)
        destination_fd = os.open(
            destination_path,
            destination_flags,
            0o700 if item_stat.st_mode & 0o111 else 0o600,
        )
        try:
            opened = os.fstat(source_fd)
            if not stat.S_ISREG(opened.st_mode):
                raise RuntimeError("seed file changed type")
            while block := os.read(source_fd, 1024 * 1024):
                view = memoryview(block)
                while view:
                    written = os.write(destination_fd, view)
                    view = view[written:]
            os.fchmod(destination_fd, 0o700 if item_stat.st_mode & 0o111 else 0o600)
        finally:
            os.close(destination_fd)
            os.close(source_fd)


def main(argv: list[str] | None = None) -> int:
    values = list(sys.argv[1:] if argv is None else argv)
    if len(values) < 3:
        print("swoon sandbox setup failed: missing launcher arguments", file=sys.stderr)
        return 126
    seed = Path(values[0])
    workspace = Path(values[1])
    command = values[2:]
    os.umask(0o077)
    try:
        _copy_tree(seed, workspace)
        os.chdir(workspace)
    except Exception as error:
        print(
            f"swoon sandbox setup failed ({error.__class__.__name__})",
            file=sys.stderr,
        )
        return 126
    os.write(sys.stdout.fileno(), _READY_MARKER)
    try:
        os.execvpe(command[0], command, os.environ)
    except FileNotFoundError:
        print(f"swoon command not found: {command[0]!r}", file=sys.stderr)
        return 127
    except OSError as error:
        print(
            f"swoon command could not start ({error.__class__.__name__})",
            file=sys.stderr,
        )
        return 126


if __name__ == "__main__":
    raise SystemExit(main())
