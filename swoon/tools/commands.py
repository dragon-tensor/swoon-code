"""Offline, shell-free foreground execution in disposable OS sandboxes."""

from __future__ import annotations

import errno
import os
import platform
import re
import select
import shlex
import shutil
import signal
import struct
import subprocess
import tempfile
import time
import unicodedata
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator

from swoon.aeml.models import (
    PathRef,
    Result,
    ResultStatus,
    Root,
    Truncation,
    ValidatedAction,
)
from swoon.policy import PathAccess, PathExistence, PathKind, PathPolicy, PathPolicyError

from .command_snapshot import CommandSnapshotBuilder, CommandSnapshotStats
from .errors import ToolExecutionError
from .models import CommandToolLimits


_SHELL_OPERATORS = frozenset({"|", "||", "&&", ";", "&", "<", ">", ">>", "2>"})
_URL_SCHEME = re.compile(r"[A-Za-z][A-Za-z0-9+.-]*://")
_TRAVERSAL = re.compile(r"(?:^|[=/])\.\.(?:/|$)")
_XML_ALLOWED_CONTROLS = {"\t", "\n", "\r"}
_SANDBOX_READY_MARKER = b"\x1eSWOON_SANDBOX_READY\x1e\n"


@dataclass(frozen=True, slots=True)
class SandboxPlatform:
    audit_arch: int
    socket_syscall: int
    reject_x32: bool = False


_PLATFORMS = {
    "x86_64": SandboxPlatform(0xC000003E, 41, reject_x32=True),
    "amd64": SandboxPlatform(0xC000003E, 41, reject_x32=True),
    "aarch64": SandboxPlatform(0xC00000B7, 198),
    "arm64": SandboxPlatform(0xC00000B7, 198),
}


class ForegroundCommandTools:
    """Run one bounded command against filtered, throw-away root snapshots."""

    def __init__(
        self,
        policy: PathPolicy,
        limits: CommandToolLimits,
        *,
        sandbox_binary: str | Path | None = None,
        resource_limiter_binary: str | Path | None = None,
        python_binary: str | Path | None = None,
    ) -> None:
        self.policy = policy
        self.limits = limits
        self.snapshot_builder = CommandSnapshotBuilder(policy, limits)
        self.sandbox_binary = self._find_binary(sandbox_binary, "bwrap")
        self.resource_limiter_binary = self._find_binary(
            resource_limiter_binary,
            "prlimit",
        )
        self.python_binary = self._find_system_python(python_binary)
        self.runner_path = Path(__file__).with_name("sandbox_runner.py").resolve()

    def run_command(self, action: ValidatedAction) -> Result:
        command = action.argument("cmd")
        if not isinstance(command, str):
            raise ToolExecutionError("invalid_command", "run-command requires text cmd")
        argv = self._parse_command(command)
        self._validate_command_paths(argv)
        return self._execute(
            action,
            argv,
            timeout=self._timeout(action, self.limits.default_timeout_seconds),
            max_output_lines=self._max_output_lines(action),
        )

    def run_build(self, action: ValidatedAction) -> Result:
        return self._run_managed(action, "build")

    def run_tests(self, action: ValidatedAction) -> Result:
        return self._run_managed(action, "tests")

    def run_linter(self, action: ValidatedAction) -> Result:
        return self._run_managed(action, "linter")

    def _run_managed(self, action: ValidatedAction, operation: str) -> Result:
        manager_value = action.argument("manager")
        manager = (
            manager_value
            if isinstance(manager_value, str)
            else self._detect_manager()
        )
        target_value = action.argument("target")
        target = target_value if isinstance(target_value, str) else None
        if target is not None:
            self._validate_target(target)
        argv = self._managed_argv(manager, operation, target)
        self._validate_command_paths(argv)
        return self._execute(
            action,
            argv,
            timeout=self._timeout(action, self.limits.managed_timeout_seconds),
            max_output_lines=self._max_output_lines(action),
        )

    def _execute(
        self,
        action: ValidatedAction,
        argv: list[str],
        *,
        timeout: int,
        max_output_lines: int,
    ) -> Result:
        self._require_runtime()
        with tempfile.TemporaryDirectory(prefix="swoon-command-") as temporary:
            snapshot_root = Path(temporary)
            output_snapshot = snapshot_root / "output"
            input_snapshot = snapshot_root / "input"
            output_snapshot.mkdir(mode=0o700)
            input_snapshot.mkdir(mode=0o700)
            stats = self.snapshot_builder.build(output_snapshot, input_snapshot)

            with self._network_filter() as filter_fd:
                command = self._sandbox_argv(
                    argv,
                    output_snapshot=output_snapshot,
                    input_snapshot=input_snapshot,
                    filter_fd=filter_fd,
                    timeout=timeout,
                )
                try:
                    process = subprocess.Popen(
                        command,
                        stdin=subprocess.DEVNULL,
                        stdout=subprocess.PIPE,
                        stderr=subprocess.STDOUT,
                        env={"PATH": "/usr/bin:/bin", "LANG": "C", "LC_ALL": "C"},
                        close_fds=True,
                        pass_fds=(filter_fd,),
                        start_new_session=True,
                    )
                except OSError as error:
                    raise ToolExecutionError(
                        "tool_unavailable",
                        f"Command sandbox could not start ({error.__class__.__name__})",
                    ) from error

                assert process.stdout is not None
                output_fd = process.stdout.fileno()
                os.set_blocking(output_fd, False)
                captured = bytearray()
                deadline = time.monotonic() + timeout
                timed_out = False
                exceeded = False
                while process.poll() is None:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        timed_out = True
                        self._kill(process)
                        break
                    readable, _, _ = select.select(
                        (output_fd,),
                        (),
                        (),
                        min(0.05, remaining),
                    )
                    if readable and self._drain_output(output_fd, captured):
                        exceeded = True
                        self._kill(process)
                        break
                if process.poll() is None:
                    self._kill(process)
                try:
                    process.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    self._kill(process)
                    process.wait()
                if not exceeded and self._drain_output(output_fd, captured):
                    exceeded = True
                process.stdout.close()
                if exceeded:
                    raise ToolExecutionError(
                        "output_limit_exceeded",
                        f"Command output exceeded {self.limits.max_capture_bytes} bytes",
                    )
                payload = bytes(captured)

            marker_at = payload.find(_SANDBOX_READY_MARKER)
            if marker_at < 0 and not timed_out:
                detail = self._sanitize_output(payload, snapshot_root).strip()
                message = "Command sandbox failed before program launch"
                if detail:
                    message += f": {detail[:500]}"
                raise ToolExecutionError("sandbox_failed", message)
            if marker_at >= 0:
                payload = payload[marker_at + len(_SANDBOX_READY_MARKER) :]
            text = self._sanitize_output(payload, snapshot_root)
            return self._command_result(
                action.source.id,
                text,
                returncode=process.returncode,
                timed_out=timed_out,
                timeout=timeout,
                max_output_lines=max_output_lines,
                snapshot_stats=stats,
            )

    def _sandbox_argv(
        self,
        argv: list[str],
        *,
        output_snapshot: Path,
        input_snapshot: Path,
        filter_fd: int,
        timeout: int,
    ) -> list[str]:
        assert self.sandbox_binary is not None
        assert self.resource_limiter_binary is not None
        assert self.python_binary is not None
        output_root = self.policy.session_paths.output_root
        input_root = self.policy.session_paths.input_root
        command = [
            str(self.resource_limiter_binary),
            f"--cpu={timeout + 1}:{timeout + 2}",
            f"--as={self.limits.address_space_bytes}",
            f"--fsize={self.limits.max_file_bytes}",
            f"--nproc={self.limits.max_processes}",
            f"--nofile={self.limits.max_open_files}",
            "--core=0",
            "--",
            str(self.sandbox_binary),
            "--unshare-user",
            "--unshare-ipc",
            "--unshare-pid",
            "--unshare-uts",
            "--unshare-cgroup-try",
            "--disable-userns",
            "--assert-userns-disabled",
            "--hostname",
            "swoon-sandbox",
            "--die-with-parent",
            "--new-session",
            "--clearenv",
        ]
        for system_path in ("/usr", "/bin", "/sbin", "/lib", "/lib64"):
            if Path(system_path).exists():
                command.extend(("--ro-bind", system_path, system_path))
        command.extend(
            (
                "--dir",
                "/etc",
                "--ro-bind-try",
                "/etc/ld.so.cache",
                "/etc/ld.so.cache",
                "--ro-bind-try",
                "/etc/localtime",
                "/etc/localtime",
                "--ro-bind-try",
                "/etc/alternatives",
                "/etc/alternatives",
                "--proc",
                "/proc",
                "--dev",
                "/dev",
                "--size",
                str(self.limits.temporary_bytes),
                "--tmpfs",
                "/tmp",
                "--dir",
                "/tmp/home",
                "--dir",
                "/output",
                "--dir",
                output_root,
                "--size",
                str(self.limits.workspace_bytes),
                "--tmpfs",
                output_root,
                "--dir",
                "/input",
                "--dir",
                input_root,
                "--ro-bind",
                str(input_snapshot),
                input_root,
                "--dir",
                "/swoon-seed",
                "--ro-bind",
                str(output_snapshot),
                "/swoon-seed",
                "--ro-bind",
                str(self.runner_path),
                "/swoon-runner.py",
                "--chdir",
                output_root,
                "--setenv",
                "PATH",
                "/usr/local/bin:/usr/bin:/bin",
                "--setenv",
                "HOME",
                "/tmp/home",
                "--setenv",
                "TMPDIR",
                "/tmp",
                "--setenv",
                "XDG_CACHE_HOME",
                "/tmp/cache",
                "--setenv",
                "LANG",
                "C.UTF-8",
                "--setenv",
                "LC_ALL",
                "C.UTF-8",
                "--setenv",
                "TZ",
                "UTC",
                "--setenv",
                "CI",
                "1",
                "--setenv",
                "PWD",
                output_root,
                "--setenv",
                "PYTHONNOUSERSITE",
                "1",
                "--setenv",
                "PIP_CONFIG_FILE",
                "/dev/null",
                "--setenv",
                "PIP_DISABLE_PIP_VERSION_CHECK",
                "1",
                "--setenv",
                "PIP_NO_INPUT",
                "1",
                "--setenv",
                "NPM_CONFIG_USERCONFIG",
                "/dev/null",
                "--setenv",
                "NPM_CONFIG_CACHE",
                "/tmp/npm-cache",
                "--setenv",
                "CARGO_HOME",
                "/tmp/cargo",
                "--setenv",
                "GOCACHE",
                "/tmp/go-cache",
                "--setenv",
                "GOMODCACHE",
                "/tmp/go-mod-cache",
                "--setenv",
                "GIT_CONFIG_GLOBAL",
                "/dev/null",
                "--setenv",
                "GIT_CONFIG_NOSYSTEM",
                "1",
                "--setenv",
                "GIT_TERMINAL_PROMPT",
                "0",
                "--seccomp",
                str(filter_fd),
                "--",
                str(self.python_binary),
                "/swoon-runner.py",
                "/swoon-seed",
                output_root,
                *argv,
            )
        )
        return command

    def _parse_command(self, command: str) -> list[str]:
        if len(command.encode("utf-8")) > self.limits.max_command_bytes:
            raise ToolExecutionError(
                "invalid_command",
                f"Command exceeds {self.limits.max_command_bytes} bytes",
            )
        if "\x00" in command or any(
            unicodedata.category(character).startswith("C")
            and character not in {"\t", "\n", "\r"}
            for character in command
        ):
            raise ToolExecutionError("invalid_command", "Command contains control characters")
        try:
            argv = shlex.split(command, posix=True)
        except ValueError as error:
            raise ToolExecutionError("invalid_command", "Command quoting is invalid") from error
        if not argv:
            raise ToolExecutionError("invalid_command", "Command cannot be empty")
        if len(argv) > self.limits.max_arguments:
            raise ToolExecutionError(
                "invalid_command",
                f"Command exceeds {self.limits.max_arguments} arguments",
            )
        for token in argv:
            if len(token.encode("utf-8")) > self.limits.max_argument_bytes:
                raise ToolExecutionError(
                    "invalid_command",
                    f"A command argument exceeds {self.limits.max_argument_bytes} bytes",
                )
            if token in _SHELL_OPERATORS:
                raise ToolExecutionError(
                    "shell_syntax_disabled",
                    "Shell operators are disabled; run one argv-based command per action",
                )
        if argv[0].startswith("-"):
            raise ToolExecutionError("invalid_command", "Command executable cannot be an option")
        return argv

    def _validate_command_paths(self, argv: list[str]) -> None:
        output_root = self.policy.session_paths.output_root
        input_root = self.policy.session_paths.input_root
        for index, token in enumerate(argv):
            candidate = token.split("=", 1)[1] if token.startswith("-") and "=" in token else token
            path_syntax_only = not any(
                character.isspace() or character in "'\"(){};"
                for character in candidate
            )
            if path_syntax_only and _URL_SCHEME.match(candidate):
                raise ToolExecutionError(
                    "network_disabled",
                    "Foreground command sandboxes do not accept network URLs",
                )
            if path_syntax_only and _TRAVERSAL.search(candidate):
                raise ToolExecutionError("path_escape", "Command path traversal is forbidden")

            if candidate.startswith("/"):
                if candidate == output_root or candidate.startswith(output_root + "/"):
                    self._authorize_logical_path(candidate, output_root, Root.OUTPUT)
                elif candidate == input_root or candidate.startswith(input_root + "/"):
                    self._authorize_logical_path(candidate, input_root, Root.INPUT)
                else:
                    raise ToolExecutionError(
                        "path_escape",
                        "Absolute command paths must use this session's virtual roots",
                    )
                continue

            path_candidate = candidate
            if path_candidate.startswith("./"):
                path_candidate = path_candidate[2:]
            looks_like_path = path_syntax_only and (
                (index == 0 and "/" in path_candidate)
                or "/" in path_candidate
                or path_candidate in {".", ".."}
            )
            try:
                if looks_like_path and path_candidate != ".":
                    if self.policy.is_denied(PathRef(path_candidate, Root.OUTPUT)):
                        raise ToolExecutionError(
                            "credential_path",
                            "Credential-shaped command paths are inaccessible",
                        )
                elif path_candidate and not path_candidate.startswith("-"):
                    # Catch denied single-component names such as .env without
                    # interpreting every ordinary positional value as a path.
                    if self.policy.denylist.denies((path_candidate,)):
                        raise ToolExecutionError(
                            "credential_path",
                            "Credential-shaped command paths are inaccessible",
                        )
            except PathPolicyError as error:
                raise ToolExecutionError(error.code, str(error)) from error

        executable = argv[0]
        if executable.startswith("./"):
            executable = executable[2:]
        if "/" in executable and not executable.startswith("/"):
            try:
                self.policy.resolve(
                    PathRef(executable, Root.OUTPUT),
                    access=PathAccess.READ,
                    existence=PathExistence.MUST_EXIST,
                    kind=PathKind.FILE,
                )
            except PathPolicyError as error:
                raise ToolExecutionError(error.code, str(error)) from error

    def _authorize_logical_path(self, value: str, prefix: str, root: Root) -> None:
        suffix = value[len(prefix) :].lstrip("/")
        reference = PathRef(suffix or ".", root)
        try:
            self.policy.resolve(
                reference,
                access=PathAccess.READ,
                existence=PathExistence.MAY_EXIST,
                kind=PathKind.ANY,
            )
        except PathPolicyError as error:
            raise ToolExecutionError(error.code, str(error)) from error

    def _validate_target(self, target: str) -> None:
        if len(target.encode("utf-8")) > self.limits.max_argument_bytes:
            raise ToolExecutionError("invalid_command", "Managed command target is too large")
        if "\x00" in target or any(
            unicodedata.category(character).startswith("C")
            for character in target
        ):
            raise ToolExecutionError(
                "invalid_command",
                "Managed command target contains control characters",
            )
        if target in _SHELL_OPERATORS:
            raise ToolExecutionError(
                "shell_syntax_disabled",
                "Managed command target cannot be a shell operator",
            )

    def _detect_manager(self) -> str:
        candidates: list[str] = []
        if any(
            self._output_file_exists(name)
            for name in ("pyproject.toml", "setup.py", "setup.cfg", "requirements.txt")
        ):
            candidates.append("pip")
        if self._output_file_exists("package.json"):
            if self._output_file_exists("pnpm-lock.yaml"):
                candidates.append("pnpm")
            elif self._output_file_exists("yarn.lock"):
                candidates.append("yarn")
            else:
                candidates.append("npm")
        for manifest, manager in (
            ("Cargo.toml", "cargo"),
            ("go.mod", "go"),
            ("Gemfile", "bundler"),
            ("composer.json", "composer"),
        ):
            if self._output_file_exists(manifest):
                candidates.append(manager)
        if not candidates:
            raise ToolExecutionError(
                "manager_not_detected",
                "No supported project manifest was found in output",
            )
        if len(candidates) > 1:
            raise ToolExecutionError(
                "manager_ambiguous",
                "Multiple project ecosystems were detected; specify manager explicitly",
            )
        return candidates[0]

    def _output_file_exists(self, name: str) -> bool:
        try:
            resolved = self.policy.resolve(
                PathRef(name, Root.OUTPUT),
                access=PathAccess.READ,
                existence=PathExistence.MAY_EXIST,
                kind=PathKind.FILE,
            )
        except PathPolicyError as error:
            raise ToolExecutionError(error.code, str(error)) from error
        return resolved.exists

    @staticmethod
    def _managed_argv(manager: str, operation: str, target: str | None) -> list[str]:
        if manager == "pip":
            commands = {
                "build": ["python3", "-m", "build"],
                "tests": ["python3", "-m", "pytest"],
                "linter": ["python3", "-m", "ruff", "check"],
            }
        elif manager in {"npm", "pnpm"}:
            script = {"build": "build", "tests": "test", "linter": "lint"}[operation]
            commands = {operation: [manager, "run", script]}
        elif manager == "yarn":
            script = {"build": "build", "tests": "test", "linter": "lint"}[operation]
            commands = {operation: ["yarn", script]}
        elif manager == "cargo":
            commands = {
                "build": ["cargo", "build"],
                "tests": ["cargo", "test"],
                "linter": ["cargo", "clippy", "--all-targets", "--all-features"],
            }
        elif manager == "go":
            commands = {
                "build": ["go", "build"],
                "tests": ["go", "test"],
                "linter": ["go", "vet"],
            }
        elif manager == "bundler":
            commands = {
                "build": ["bundle", "exec", "rake", "build"],
                "tests": ["bundle", "exec", "rake", "test"],
                "linter": ["bundle", "exec", "rubocop"],
            }
        elif manager == "composer":
            script = {"build": "build", "tests": "test", "linter": "lint"}[operation]
            commands = {operation: ["composer", "run-script", script]}
        else:
            raise ToolExecutionError("invalid_command", "Unknown managed command ecosystem")

        argv = list(commands[operation])
        if target is not None:
            if manager in {"npm", "pnpm", "yarn", "composer"}:
                argv.append("--")
            argv.append(target)
        elif manager == "go":
            argv.append("./...")
        elif manager == "bundler" and operation == "linter":
            argv.append(".")
        return argv

    @staticmethod
    def _timeout(action: ValidatedAction, default: int) -> int:
        value = action.argument("timeout")
        return value if isinstance(value, int) else default

    def _max_output_lines(self, action: ValidatedAction) -> int:
        value = action.argument("max_output_lines")
        return value if isinstance(value, int) else self.limits.default_output_lines

    def _command_result(
        self,
        action_id: str,
        output: str,
        *,
        returncode: int,
        timed_out: bool,
        timeout: int,
        max_output_lines: int,
        snapshot_stats: CommandSnapshotStats,
    ) -> Result:
        output_lines = output.splitlines(keepends=True)
        line_limited = len(output_lines) > max_output_lines
        visible_output = "".join(output_lines[:max_output_lines])
        exit_value = "timeout" if timed_out else str(returncode)
        metadata = (
            f"exit_code={exit_value}\n"
            "workspace_changes=discarded\n"
            f"denied_paths_omitted={snapshot_stats.denied_entries}\n"
        )
        if timed_out:
            metadata += f"timeout_seconds={timeout}\n"
        full_body = metadata + output
        visible_body = metadata + visible_output
        total_bytes = len(full_body.encode("utf-8"))
        body = self._truncate_utf8(visible_body, self.limits.max_result_bytes)
        byte_limited = len(body.encode("utf-8")) < len(visible_body.encode("utf-8"))
        truncated = line_limited or byte_limited

        if timed_out:
            status = ResultStatus.TIMEOUT
        elif returncode != 0:
            status = ResultStatus.FAILURE
        elif truncated:
            status = ResultStatus.PARTIAL
        else:
            status = ResultStatus.SUCCESS
        return Result(
            action_id=action_id,
            status=status,
            body=body,
            lines=(
                f"1-{min(len(output_lines), max_output_lines)}"
                if output_lines
                else None
            ),
            truncation=(
                Truncation(total_bytes=total_bytes, offset=0) if truncated else None
            ),
        )

    def _sanitize_output(self, payload: bytes, snapshot_root: Path) -> str:
        text = payload.decode("utf-8", errors="replace")
        replacements = (
            (str(snapshot_root), "[sandbox]"),
            (str(self.policy.session_paths.host_root), "[session]"),
            (str(self.policy.session_paths.host_output), self.policy.session_paths.output_root),
            (str(self.policy.session_paths.host_input), self.policy.session_paths.input_root),
            (str(self.runner_path), "/swoon-runner.py"),
        )
        for physical, logical in sorted(replacements, key=lambda item: len(item[0]), reverse=True):
            text = text.replace(physical, logical)
        return "".join(
            character
            if character in _XML_ALLOWED_CONTROLS
            or not unicodedata.category(character).startswith("C")
            else "\ufffd"
            for character in text
        )

    @staticmethod
    def _truncate_utf8(text: str, maximum: int) -> str:
        payload = text.encode("utf-8")
        if len(payload) <= maximum:
            return text
        payload = payload[:maximum]
        while payload:
            try:
                return payload.decode("utf-8")
            except UnicodeDecodeError as error:
                if error.reason == "unexpected end of data":
                    payload = payload[: error.start]
                    continue
                return payload.decode("utf-8", errors="replace")
        return ""

    @contextmanager
    def _network_filter(self) -> Iterator[int]:
        system = _PLATFORMS.get(platform.machine().lower())
        if os.name != "posix" or platform.system() != "Linux" or system is None:
            raise ToolExecutionError(
                "platform_unsupported",
                "Foreground command sandboxing requires supported 64-bit Linux",
            )
        if not hasattr(os, "memfd_create"):
            raise ToolExecutionError(
                "platform_unsupported",
                "Anonymous seccomp filter files are unavailable",
            )

        instructions = [
            (0x20, 0, 0, 4),
            (0x15, 1, 0, system.audit_arch),
            (0x06, 0, 0, 0x80000000),
            (0x20, 0, 0, 0),
        ]
        denied = 0x00050000 | errno.EPERM
        if system.reject_x32:
            instructions.extend(
                (
                    (0x35, 0, 1, 0x40000000),
                    (0x06, 0, 0, denied),
                )
            )
        instructions.extend(
            (
                (0x15, 0, 1, system.socket_syscall),
                (0x06, 0, 0, denied),
                (0x06, 0, 0, 0x7FFF0000),
            )
        )
        descriptor = os.memfd_create(
            "swoon-seccomp",
            getattr(os, "MFD_CLOEXEC", 0x0001),
        )
        try:
            payload = b"".join(
                struct.pack("=HBBI", code, jump_true, jump_false, value)
                for code, jump_true, jump_false, value in instructions
            )
            os.write(descriptor, payload)
            os.lseek(descriptor, 0, os.SEEK_SET)
            yield descriptor
        finally:
            os.close(descriptor)

    def _require_runtime(self) -> None:
        if os.name != "posix" or platform.system() != "Linux":
            raise ToolExecutionError(
                "platform_unsupported",
                "Foreground command sandboxing requires Linux",
            )
        if self.sandbox_binary is None or self.resource_limiter_binary is None:
            raise ToolExecutionError(
                "tool_unavailable",
                "Bubblewrap and prlimit are required for foreground commands",
            )
        if self.python_binary is None or not self.runner_path.is_file():
            raise ToolExecutionError(
                "tool_unavailable",
                "A system Python sandbox launcher is unavailable",
            )

    @staticmethod
    def _find_binary(supplied: str | Path | None, name: str) -> Path | None:
        candidate = Path(supplied) if supplied is not None else (
            Path(located) if (located := shutil.which(name)) is not None else None
        )
        if candidate is None:
            return None
        try:
            resolved = candidate.expanduser().resolve(strict=True)
        except OSError:
            return None
        return resolved if resolved.is_file() and os.access(resolved, os.X_OK) else None

    @classmethod
    def _find_system_python(cls, supplied: str | Path | None) -> Path | None:
        if supplied is not None:
            return cls._find_binary(supplied, "python3")
        for candidate in (Path("/usr/bin/python3"), Path("/usr/local/bin/python3")):
            found = cls._find_binary(candidate, "python3")
            if found is not None:
                return found
        return None

    @staticmethod
    def _kill(process: subprocess.Popen) -> None:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        except OSError:
            try:
                process.kill()
            except ProcessLookupError:
                pass

    def _drain_output(self, descriptor: int, captured: bytearray) -> bool:
        """Drain a non-blocking pipe while retaining at most limit + one byte."""

        maximum = self.limits.max_capture_bytes
        while True:
            try:
                block = os.read(descriptor, 64 * 1024)
            except BlockingIOError:
                return len(captured) > maximum
            if not block:
                return len(captured) > maximum
            remaining = maximum + 1 - len(captured)
            if remaining > 0:
                captured.extend(block[:remaining])
            if len(block) > remaining or len(captured) > maximum:
                return True
