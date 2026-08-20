"""Dependency inspection by parsing known project manifests without executing package tools."""

from __future__ import annotations

import json
import re
import tomllib
import unicodedata
from collections.abc import Mapping
from typing import Any, BinaryIO

from swoon.aeml.models import PathRef, Result, Root, ValidatedAction
from swoon.policy import PathAccess, PathExistence, PathKind, PathPolicy, PathPolicyError

from .errors import ToolExecutionError
from .models import ReadToolLimits
from .output import OutputCollector
from .safe_io import SafeIO


_REQUIREMENTS_FILE = re.compile(r"requirements(?:[-_.][A-Za-z0-9_.-]+)?\.txt\Z", re.IGNORECASE)
_URL_USERINFO = re.compile(r"(?i)([a-z][a-z0-9+.-]*://)[^/@\s]+@")
_SECRET_QUERY = re.compile(
    r"(?i)([?&](?:token|access_token|api_key|key|password|auth)=)[^&\s]+"
)
_GEM_LINE = re.compile(r"^\s*gem\s+[\"']([^\"']+)[\"'](?:\s*,\s*[\"']([^\"']+)[\"'])?")


class DependencyReadTools:
    def __init__(self, policy: PathPolicy, limits: ReadToolLimits) -> None:
        self.policy = policy
        self.limits = limits
        self.io = SafeIO(policy)

    def list_dependencies(self, action: ValidatedAction) -> Result:
        manager_value = action.argument("manager")
        requested = manager_value if isinstance(manager_value, str) else None
        lines: list[str] = []
        found = False

        if requested in {None, "pip"}:
            pyproject = self._optional_bytes("pyproject.toml")
            if pyproject is not None:
                found = True
                lines.extend(self._parse_python_project(pyproject))
            for name, payload in self._requirements_files():
                found = True
                lines.extend(self._parse_requirements(name, payload))

        if requested in {None, "npm", "pnpm", "yarn"}:
            package_json = self._optional_bytes("package.json")
            if package_json is not None:
                found = True
                js_manager = requested or self._detect_js_manager()
                lines.extend(self._parse_package_json(package_json, js_manager))

        if requested in {None, "cargo"}:
            cargo = self._optional_bytes("Cargo.toml")
            if cargo is not None:
                found = True
                lines.extend(self._parse_cargo(cargo))

        if requested in {None, "go"}:
            go_mod = self._optional_bytes("go.mod")
            if go_mod is not None:
                found = True
                lines.extend(self._parse_go_mod(go_mod))

        if requested in {None, "composer"}:
            composer = self._optional_bytes("composer.json")
            if composer is not None:
                found = True
                lines.extend(self._parse_composer(composer))

        if requested in {None, "bundler"}:
            gem_lock = self._optional_bytes("Gemfile.lock")
            gemfile = None if gem_lock is not None else self._optional_bytes("Gemfile")
            if gem_lock is not None or gemfile is not None:
                found = True
                lines.extend(
                    self._parse_gem_lock(gem_lock)
                    if gem_lock is not None
                    else self._parse_gemfile(gemfile or b"")
                )

        if not found:
            suffix = f" for manager {requested}" if requested else ""
            lines.append(f"No dependency manifests found{suffix}.")

        collector = OutputCollector(self.limits.max_output_bytes)
        for line in sorted(set(lines), key=str.casefold):
            collector.add(line + "\n")
        return collector.result(action.source.id)

    def _optional_bytes(self, relative_path: str) -> bytes | None:
        try:
            resolved = self.policy.resolve(
                PathRef(relative_path, Root.OUTPUT),
                access=PathAccess.READ,
                existence=PathExistence.MAY_EXIST,
                kind=PathKind.FILE,
            )
        except PathPolicyError as error:
            raise ToolExecutionError(error.code, str(error)) from error
        if not resolved.exists:
            return None
        with self.io.open_file(resolved) as stream:
            return self._read_manifest(stream, relative_path)

    def _requirements_files(self) -> list[tuple[str, bytes]]:
        try:
            root = self.policy.resolve(
                PathRef(".", Root.OUTPUT),
                access=PathAccess.READ,
                existence=PathExistence.MUST_EXIST,
                kind=PathKind.DIRECTORY,
            )
        except PathPolicyError as error:
            raise ToolExecutionError(error.code, str(error)) from error
        files: list[tuple[str, bytes]] = []
        with self.io.open_directory(root) as directory_fd:
            for entry in self.io.entries(directory_fd):
                if not entry.is_file or not _REQUIREMENTS_FILE.fullmatch(entry.name):
                    continue
                if self.policy.denylist.denies((entry.name,)):
                    continue
                with self.io.open_child_file(directory_fd, entry) as stream:
                    files.append((entry.name, self._read_manifest(stream, entry.name)))
        return files

    def _read_manifest(self, stream: BinaryIO, name: str) -> bytes:
        payload = stream.read(self.limits.max_manifest_bytes + 1)
        if len(payload) > self.limits.max_manifest_bytes:
            raise ToolExecutionError(
                "tool_failed",
                f"Dependency manifest {name} exceeds its size limit",
            )
        if b"\x00" in payload:
            raise ToolExecutionError("binary_unsupported", f"Dependency manifest {name} is binary")
        return payload

    def _parse_python_project(self, payload: bytes) -> list[str]:
        data = self._toml(payload, "pyproject.toml")
        lines: list[str] = []
        project = data.get("project")
        if isinstance(project, Mapping):
            lines.extend(self._string_list("pip runtime", project.get("dependencies")))
            optional = project.get("optional-dependencies")
            if isinstance(optional, Mapping):
                for group, dependencies in optional.items():
                    if isinstance(group, str):
                        lines.extend(
                            self._string_list(
                                f"pip optional:{self._redact(group)}",
                                dependencies,
                            )
                        )

        tool = data.get("tool")
        poetry = tool.get("poetry") if isinstance(tool, Mapping) else None
        if isinstance(poetry, Mapping):
            dependencies = poetry.get("dependencies")
            if isinstance(dependencies, Mapping):
                lines.extend(
                    self._mapping_dependencies(
                        "pip poetry",
                        dependencies,
                        skip={"python"},
                    )
                )
            groups = poetry.get("group")
            if isinstance(groups, Mapping):
                for group, group_value in groups.items():
                    if isinstance(group, str) and isinstance(group_value, Mapping):
                        group_dependencies = group_value.get("dependencies")
                        if isinstance(group_dependencies, Mapping):
                            lines.extend(
                                self._mapping_dependencies(
                                    f"pip poetry:{self._redact(group)}",
                                    group_dependencies,
                                )
                            )
        return lines

    def _parse_requirements(self, name: str, payload: bytes) -> list[str]:
        text = self._utf8(payload, name)
        output: list[str] = []
        for raw_line in text.splitlines():
            line = raw_line.strip()
            if not line or line.startswith("#") or line.startswith("-"):
                continue
            requirement = line.split(" #", 1)[0].strip()
            output.append(f"pip {name} {self._redact(requirement)}")
        return output

    def _parse_package_json(self, payload: bytes, manager: str) -> list[str]:
        data = self._json(payload, "package.json")
        output: list[str] = []
        sections = (
            ("dependencies", "runtime"),
            ("devDependencies", "dev"),
            ("peerDependencies", "peer"),
            ("optionalDependencies", "optional"),
        )
        for key, scope in sections:
            dependencies = data.get(key)
            if isinstance(dependencies, Mapping):
                for name, value in dependencies.items():
                    if isinstance(name, str) and isinstance(value, str):
                        output.append(
                            f"{manager} {scope} {self._redact(name)} {self._redact(value)}"
                        )
        return output

    def _parse_cargo(self, payload: bytes) -> list[str]:
        data = self._toml(payload, "Cargo.toml")
        output: list[str] = []
        for key, scope in (
            ("dependencies", "runtime"),
            ("dev-dependencies", "dev"),
            ("build-dependencies", "build"),
        ):
            dependencies = data.get(key)
            if isinstance(dependencies, Mapping):
                output.extend(self._mapping_dependencies(f"cargo {scope}", dependencies))
        return output

    def _parse_go_mod(self, payload: bytes) -> list[str]:
        text = self._utf8(payload, "go.mod")
        output: list[str] = []
        in_require = False
        for raw_line in text.splitlines():
            line = raw_line.split("//", 1)[0].strip()
            if line == "require (":
                in_require = True
                continue
            if in_require and line == ")":
                in_require = False
                continue
            if line.startswith("require "):
                line = line[len("require ") :].strip()
            elif not in_require:
                continue
            fields = line.split()
            if len(fields) >= 2:
                output.append(
                    f"go runtime {self._redact(fields[0])} {self._redact(fields[1])}"
                )
        return output

    def _parse_composer(self, payload: bytes) -> list[str]:
        data = self._json(payload, "composer.json")
        output: list[str] = []
        for key, scope in (("require", "runtime"), ("require-dev", "dev")):
            dependencies = data.get(key)
            if isinstance(dependencies, Mapping):
                for name, value in dependencies.items():
                    if isinstance(name, str) and isinstance(value, str):
                        output.append(
                            f"composer {scope} {self._redact(name)} {self._redact(value)}"
                        )
        return output

    def _parse_gem_lock(self, payload: bytes) -> list[str]:
        text = self._utf8(payload, "Gemfile.lock")
        output: list[str] = []
        in_dependencies = False
        for raw_line in text.splitlines():
            if raw_line == "DEPENDENCIES":
                in_dependencies = True
                continue
            if in_dependencies and raw_line and not raw_line.startswith(" "):
                break
            if in_dependencies:
                value = raw_line.strip()
                if value:
                    output.append(f"bundler runtime {self._redact(value)}")
        return output

    def _parse_gemfile(self, payload: bytes) -> list[str]:
        text = self._utf8(payload, "Gemfile")
        output: list[str] = []
        for line in text.splitlines():
            match = _GEM_LINE.match(line)
            if match:
                name, version = match.groups()
                suffix = f" {self._redact(version)}" if version else ""
                output.append(f"bundler runtime {self._redact(name)}{suffix}")
        return output

    def _detect_js_manager(self) -> str:
        for filename, manager in (
            ("pnpm-lock.yaml", "pnpm"),
            ("yarn.lock", "yarn"),
            ("package-lock.json", "npm"),
        ):
            if self._file_exists(filename):
                return manager
        return "npm"

    def _file_exists(self, relative_path: str) -> bool:
        try:
            resolved = self.policy.resolve(
                PathRef(relative_path, Root.OUTPUT),
                access=PathAccess.READ,
                existence=PathExistence.MAY_EXIST,
                kind=PathKind.FILE,
            )
        except PathPolicyError as error:
            raise ToolExecutionError(error.code, str(error)) from error
        return resolved.exists

    def _mapping_dependencies(
        self,
        prefix: str,
        values: Mapping[Any, Any],
        *,
        skip: set[str] | None = None,
    ) -> list[str]:
        output: list[str] = []
        for name, value in values.items():
            if not isinstance(name, str) or (skip and name in skip):
                continue
            rendered = self._dependency_value(value)
            output.append(
                f"{prefix} {self._redact(name)}{(' ' + rendered) if rendered else ''}"
            )
        return output

    def _dependency_value(self, value: Any) -> str:
        if isinstance(value, str):
            return self._redact(value)
        if isinstance(value, Mapping):
            fields: list[str] = []
            for key in ("version", "git", "path", "branch", "tag", "rev"):
                item = value.get(key)
                if isinstance(item, str):
                    fields.append(f"{key}={self._redact(item)}")
            return " ".join(fields)
        if isinstance(value, bool):
            return str(value).lower()
        return ""

    def _string_list(self, prefix: str, value: Any) -> list[str]:
        if not isinstance(value, list):
            return []
        return [f"{prefix} {self._redact(item)}" for item in value if isinstance(item, str)]

    @staticmethod
    def _json(payload: bytes, name: str) -> dict[str, Any]:
        def no_duplicates(pairs):
            result = {}
            for key, value in pairs:
                if key in result:
                    raise ValueError(f"duplicate key {key!r}")
                result[key] = value
            return result

        try:
            parsed = json.loads(payload.decode("utf-8"), object_pairs_hook=no_duplicates)
        except (UnicodeDecodeError, json.JSONDecodeError, ValueError, RecursionError) as error:
            raise ToolExecutionError("tool_failed", f"Invalid {name} manifest") from error
        if not isinstance(parsed, dict):
            raise ToolExecutionError("tool_failed", f"Invalid {name} manifest")
        return parsed

    @staticmethod
    def _toml(payload: bytes, name: str) -> dict[str, Any]:
        try:
            return tomllib.loads(payload.decode("utf-8"))
        except (UnicodeDecodeError, tomllib.TOMLDecodeError, RecursionError) as error:
            raise ToolExecutionError("tool_failed", f"Invalid {name} manifest") from error

    @staticmethod
    def _utf8(payload: bytes, name: str) -> str:
        try:
            return payload.decode("utf-8")
        except UnicodeDecodeError as error:
            raise ToolExecutionError("binary_unsupported", f"{name} is not UTF-8 text") from error

    @staticmethod
    def _redact(value: str) -> str:
        value = _URL_USERINFO.sub(r"\1[redacted]@", value)
        value = _SECRET_QUERY.sub(r"\1[redacted]", value)
        return "".join(
            " " if unicodedata.category(character).startswith("C") else character
            for character in value
        ).strip()
