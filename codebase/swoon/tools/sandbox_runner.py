"""Trusted in-sandbox launcher for disposable command workspaces.

This module is executed inside Bubblewrap. It is intentionally standalone: it
copies the interpreter-filtered output seed into the size-limited tmpfs and
then supervises the requested argv without invoking a shell. The supervisor is
PID 1 in the private namespace and enforces a task count independent of the
host user's existing desktop and browser processes.
"""

from __future__ import annotations

import os
import stat
import subprocess
import sys
import time
from pathlib import Path


_READY_MARKER = b"\x1eSWOON_SANDBOX_READY\x1e\n"
_PROCESS_POLL_SECONDS = 0.02


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


def _task_count(proc_root: Path = Path("/proc")) -> int:
    total = 0
    try:
        entries = proc_root.iterdir()
    except OSError:
        return 0
    for entry in entries:
        if not entry.name.isdecimal():
            continue
        try:
            status = (entry / "status").read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        threads = 1
        for line in status.splitlines():
            if not line.startswith("Threads:"):
                continue
            fields = line.split()
            if len(fields) == 2:
                try:
                    threads = max(1, int(fields[1]))
                except ValueError:
                    pass
            break
        total += threads
    return total


def _command_task_count() -> int:
    try:
        own_threads = len(tuple((Path("/proc") / str(os.getpid()) / "task").iterdir()))
    except OSError:
        own_threads = 1
    return max(0, _task_count() - own_threads)


def _supervise(command: list[str], max_processes: int) -> int:
    try:
        process = subprocess.Popen(command, env=os.environ)
    except FileNotFoundError:
        print(f"swoon command not found: {command[0]!r}", file=sys.stderr)
        return 127
    except OSError as error:
        print(
            f"swoon command could not start ({error.__class__.__name__})",
            file=sys.stderr,
        )
        return 126

    while (returncode := process.poll()) is None:
        if _command_task_count() > max_processes:
            print(
                f"swoon command exceeded the {max_processes}-task sandbox limit",
                file=sys.stderr,
            )
            try:
                process.kill()
                process.wait(timeout=1)
            except (OSError, subprocess.TimeoutExpired):
                pass
            return 125
        time.sleep(_PROCESS_POLL_SECONDS)
    return returncode if returncode >= 0 else 128 + abs(returncode)


def main(argv: list[str] | None = None) -> int:
    values = list(sys.argv[1:] if argv is None else argv)
    if len(values) < 4:
        print("swoon sandbox setup failed: missing launcher arguments", file=sys.stderr)
        return 126
    seed = Path(values[0])
    workspace = Path(values[1])
    try:
        max_processes = int(values[2])
    except ValueError:
        print("swoon sandbox setup failed: invalid task limit", file=sys.stderr)
        return 126
    if max_processes < 1:
        print("swoon sandbox setup failed: invalid task limit", file=sys.stderr)
        return 126
    command = values[3:]
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
    return _supervise(command, max_processes)


if __name__ == "__main__":
    raise SystemExit(main())
