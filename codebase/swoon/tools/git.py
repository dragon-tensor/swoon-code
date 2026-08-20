"""Hardened, fixed-argument read-only Git inspection."""

from __future__ import annotations

import os
import shutil
import signal
import subprocess
import tempfile
import time
import unicodedata
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from swoon.aeml.models import PathRef, Result, Root, ValidatedAction
from swoon.policy import PathAccess, PathExistence, PathKind, PathPolicy, PathPolicyError

from .errors import ToolExecutionError
from .models import ReadToolLimits
from .output import bounded_result
from .snapshot import GitSnapshotBuilder


@dataclass(frozen=True, slots=True)
class GitCommandResult:
    returncode: int
    stdout: bytes
    stderr: bytes


class GitReadTools:
    def __init__(
        self,
        policy: PathPolicy,
        limits: ReadToolLimits,
        *,
        git_binary: str | Path | None = None,
    ) -> None:
        self.policy = policy
        self.limits = limits
        candidate = Path(git_binary) if git_binary is not None else self._discover_git()
        try:
            self.git_binary = candidate.expanduser().resolve(strict=True)
        except OSError as error:
            raise ToolExecutionError("tool_unavailable", "Git executable is unavailable") from error
        if not self.git_binary.is_file() or not self.git_binary.is_absolute():
            raise ToolExecutionError("tool_unavailable", "Git executable is unavailable")
        self.snapshot_builder = GitSnapshotBuilder(policy, limits)

    def status(self, action: ValidatedAction) -> Result:
        with self._snapshot() as repository:
            command = self._run(
                repository,
                ["status", "--porcelain=v1", "-z", "--branch", "--untracked-files=all"],
            )
            self._require_success(command, "git status", repository)
            text = self._format_status(command.stdout)
        return bounded_result(
            action.source.id,
            text,
            max_bytes=self.limits.max_output_bytes,
        )

    def diff(self, action: ValidatedAction) -> Result:
        staged = action.argument("staged") is True
        max_lines_value = action.argument("max_output_lines")
        max_lines = max_lines_value if isinstance(max_lines_value, int) else None
        scope = action.source.path
        if scope is not None:
            try:
                self.policy.resolve(
                    scope,
                    access=PathAccess.READ,
                    existence=PathExistence.MAY_EXIST,
                    kind=PathKind.ANY,
                )
            except PathPolicyError as error:
                raise ToolExecutionError(error.code, str(error)) from error

        base = ["diff"]
        if staged:
            base.append("--cached")
        safeguards = ["--no-renames", "--no-ext-diff", "--no-textconv"]
        scope_arguments = [] if scope is None or scope.value == "." else ["--", scope.value]
        with self._snapshot() as repository:
            names_result = self._run(
                repository,
                base + safeguards + ["--name-only", "-z"] + scope_arguments,
            )
            self._require_success(names_result, "git diff", repository)
            safe_names = self._safe_changed_paths(names_result.stdout)
            if not safe_names:
                text = ""
            else:
                total_name_bytes = sum(len(name.encode("utf-8")) + 1 for name in safe_names)
                if len(safe_names) > 4_096 or total_name_bytes > 256 * 1024:
                    raise ToolExecutionError(
                        "tool_failed",
                        "Git diff contains too many changed paths",
                    )
                diff_result = self._run(
                    repository,
                    base + safeguards + ["--"] + safe_names,
                )
                self._require_success(diff_result, "git diff", repository)
                text = self._decode_output(diff_result.stdout, "Git diff output")
                if max_lines is not None:
                    text = "".join(text.splitlines(keepends=True)[:max_lines])
        return bounded_result(
            action.source.id,
            text,
            max_bytes=self.limits.max_output_bytes,
        )

    def log(self, action: ValidatedAction) -> Result:
        max_count_value = action.argument("max_count")
        max_count = max_count_value if isinstance(max_count_value, int) else 20
        with self._snapshot() as repository:
            head = self._run(repository, ["rev-parse", "--verify", "HEAD"])
            if head.returncode != 0:
                text = ""
            else:
                result = self._run(
                    repository,
                    [
                        "log",
                        f"--max-count={max_count}",
                        "--encoding=UTF-8",
                        "--format=%x1e%H%x1f%aI%x1f%an%x1f%s",
                    ],
                )
                self._require_success(result, "git log", repository)
                text = self._format_log(result.stdout)
        return bounded_result(
            action.source.id,
            text,
            max_bytes=self.limits.max_output_bytes,
        )

    @contextmanager
    def _snapshot(self):
        with tempfile.TemporaryDirectory(prefix="swoon-git-") as temporary:
            root = Path(temporary)
            self.snapshot_builder.build(root)
            yield root

    def _run(self, repository: Path, arguments: list[str]) -> GitCommandResult:
        command = [
            str(self.git_binary),
            "--no-pager",
            "-c",
            "color.ui=false",
            "-c",
            "core.fsmonitor=false",
            "-c",
            "core.hooksPath=",
            "-c",
            "diff.external=",
            "-C",
            str(repository),
            *arguments,
        ]
        environment = {
            "LC_ALL": "C",
            "LANG": "C",
            "GIT_CONFIG": os.devnull,
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_ATTR_NOSYSTEM": "1",
            "GIT_OPTIONAL_LOCKS": "0",
            "GIT_TERMINAL_PROMPT": "0",
            "GIT_PAGER": "cat",
            "PAGER": "cat",
        }
        if os.name == "nt":
            for name in ("SystemRoot", "WINDIR"):
                if name in os.environ:
                    environment[name] = os.environ[name]

        with tempfile.TemporaryFile() as stdout_file, tempfile.TemporaryFile() as stderr_file:
            process = subprocess.Popen(
                command,
                stdin=subprocess.DEVNULL,
                stdout=stdout_file,
                stderr=stderr_file,
                env=environment,
                close_fds=True,
                start_new_session=os.name != "nt",
                creationflags=(
                    getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0) if os.name == "nt" else 0
                ),
            )
            deadline = time.monotonic() + self.limits.git_timeout_seconds
            exceeded = False
            while process.poll() is None:
                if time.monotonic() >= deadline:
                    self._kill(process)
                    process.wait()
                    raise ToolExecutionError("timeout", "Git inspection timed out")
                if (
                    os.fstat(stdout_file.fileno()).st_size > self.limits.max_git_capture_bytes
                    or os.fstat(stderr_file.fileno()).st_size > self.limits.max_git_capture_bytes
                ):
                    exceeded = True
                    self._kill(process)
                    process.wait()
                    break
                time.sleep(0.01)
            if exceeded:
                raise ToolExecutionError("tool_failed", "Git output exceeded its safety limit")
            stdout_file.seek(0)
            stderr_file.seek(0)
            stdout = stdout_file.read(self.limits.max_git_capture_bytes + 1)
            stderr = stderr_file.read(self.limits.max_git_capture_bytes + 1)
        if (
            len(stdout) > self.limits.max_git_capture_bytes
            or len(stderr) > self.limits.max_git_capture_bytes
        ):
            raise ToolExecutionError("tool_failed", "Git output exceeded its safety limit")
        return GitCommandResult(process.returncode, stdout, stderr)

    @staticmethod
    def _kill(process: subprocess.Popen) -> None:
        try:
            if os.name != "nt":
                os.killpg(process.pid, signal.SIGKILL)
            else:
                process.kill()
        except ProcessLookupError:
            pass

    @staticmethod
    def _discover_git() -> Path:
        located = shutil.which("git")
        if located is None:
            raise ToolExecutionError("tool_unavailable", "Git executable is unavailable")
        return Path(located)

    def _safe_changed_paths(self, payload: bytes) -> list[str]:
        paths: list[str] = []
        for raw_path in payload.split(b"\x00"):
            if not raw_path:
                continue
            path = self._decode_output(raw_path, "Git path")
            try:
                reference = PathRef(path, Root.OUTPUT)
                if self.policy.is_denied(reference):
                    continue
            except PathPolicyError:
                continue
            paths.append(path)
        return paths

    def _format_status(self, payload: bytes) -> str:
        fields = payload.split(b"\x00")
        output: list[str] = []
        index = 0
        while index < len(fields):
            raw = fields[index]
            index += 1
            if not raw:
                continue
            text = self._decode_output(raw, "Git status output")
            if text.startswith("## "):
                output.append(self._sanitize_line(text) + "\n")
                continue
            if len(text) < 4:
                continue
            status = text[:2]
            path = text[3:]
            old_path = None
            if "R" in status or "C" in status:
                if index >= len(fields):
                    raise ToolExecutionError("tool_failed", "Git returned malformed status output")
                old_path = self._decode_output(fields[index], "Git status path")
                index += 1
            if not self._status_path_visible(path) or (
                old_path is not None and not self._status_path_visible(old_path)
            ):
                continue
            if old_path is None:
                output.append(f"{status} {path}\n")
            else:
                output.append(f"{status} {old_path} -> {path}\n")
        return "".join(output)

    def _status_path_visible(self, path: str) -> bool:
        try:
            return not self.policy.is_denied(PathRef(path, Root.OUTPUT))
        except PathPolicyError:
            return False

    def _format_log(self, payload: bytes) -> str:
        text = self._decode_output(payload, "Git log output")
        output: list[str] = []
        for record in text.split("\x1e"):
            record = record.strip("\r\n")
            if not record:
                continue
            fields = record.split("\x1f", 3)
            if len(fields) != 4:
                raise ToolExecutionError("tool_failed", "Git returned malformed log output")
            commit, timestamp, author, subject = fields
            output.append(
                f"{self._sanitize_line(commit)}\t{self._sanitize_line(timestamp)}\t"
                f"{self._sanitize_line(author)}\t{self._sanitize_line(subject)}\n"
            )
        return "".join(output)

    def _require_success(
        self,
        result: GitCommandResult,
        operation: str,
        repository: Path,
    ) -> None:
        if result.returncode == 0:
            return
        detail = self._decode_output(result.stderr, "Git error").strip()
        detail = detail.replace(str(repository), self.policy.session_paths.output_root)
        detail = detail.replace(
            str(self.policy.session_paths.host_output),
            self.policy.session_paths.output_root,
        )
        detail = self._sanitize_line(detail)[:500]
        message = f"{operation} failed"
        if detail:
            message += f": {detail}"
        raise ToolExecutionError("tool_failed", message, retryable=True)

    @staticmethod
    def _decode_output(payload: bytes, label: str) -> str:
        try:
            return payload.decode("utf-8")
        except UnicodeDecodeError as error:
            raise ToolExecutionError("tool_failed", f"{label} is not valid UTF-8") from error

    @staticmethod
    def _sanitize_line(text: str) -> str:
        return "".join(
            " " if unicodedata.category(character).startswith("C") else character
            for character in text
        ).strip()
