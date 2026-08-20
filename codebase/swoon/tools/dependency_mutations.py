"""Guarded dependency declaration changes without package execution or network access."""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from swoon.aeml.models import Result, ResultStatus, Root, ValidatedAction
from swoon.aeml.models import PathRef
from swoon.policy import PathAccess, PathExistence, PathKind, PathPolicy, PathPolicyError
from swoon.policy.models import ResolvedPath

from .dependencies import DependencyReadTools
from .errors import ToolExecutionError
from .models import MutationToolLimits, ReadToolLimits
from .safe_io import SafeIO, SafeMutationIO


_REQUIREMENTS_FILE = re.compile(
    r"requirements(?:[-_.][A-Za-z0-9_.-]+)?\.txt\Z",
    re.IGNORECASE,
)
_PIP_INSTALL = re.compile(
    r"(?P<name>[A-Za-z0-9][A-Za-z0-9._-]*)"
    r"(?P<extras>\[[A-Za-z0-9._-]+(?:,[A-Za-z0-9._-]+)*\])?"
    r"==(?P<version>[A-Za-z0-9][A-Za-z0-9.!+_-]*)\Z"
)
_PIP_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*\Z")
_PIP_DECLARATION_NAME = re.compile(
    r"^\s*(?P<name>[A-Za-z0-9][A-Za-z0-9._-]*)"
    r"(?:\[[A-Za-z0-9._,-]+\])?(?=\s|[<>=!~;@]|\Z)"
)
_PLAIN_PACKAGE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*\Z")
_NPM_NAME = re.compile(
    r"(?:@[a-z0-9][a-z0-9._-]*/)?[a-z0-9][a-z0-9._-]*\Z"
)
_SEMVER = re.compile(
    r"(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)"
    r"(?:-[0-9A-Za-z.-]+)?(?:\+[0-9A-Za-z.-]+)?\Z"
)
_GO_MODULE = re.compile(
    r"[A-Za-z0-9][A-Za-z0-9.-]*(?:/[A-Za-z0-9][A-Za-z0-9._~+/-]*)+\Z"
)
_GO_VERSION = re.compile(
    r"v(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)"
    r"(?:-[0-9A-Za-z.-]+)?(?:\+[0-9A-Za-z.-]+)?\Z"
)
_COMPOSER_NAME = re.compile(
    r"[a-z0-9][a-z0-9._-]*/[a-z0-9][a-z0-9._-]*\Z"
)
_TOML_HEADER = re.compile(r"(?m)^[ \t]*\[{1,2}[^\r\n]+?\]{1,2}[ \t]*(?:#.*)?$")
_GEM_DECLARATION = re.compile(
    r"^\s*gem\s+[\"'](?P<name>[^\"']+)[\"']"
    r"(?:\s*,\s*[\"'](?P<version>[^\"']+)[\"'])?.*$"
)

_LOCKFILES = {
    "pip": ("poetry.lock", "uv.lock", "Pipfile.lock"),
    "npm": ("package-lock.json", "npm-shrinkwrap.json", "pnpm-lock.yaml", "yarn.lock"),
    "pnpm": ("package-lock.json", "npm-shrinkwrap.json", "pnpm-lock.yaml", "yarn.lock"),
    "yarn": ("package-lock.json", "npm-shrinkwrap.json", "pnpm-lock.yaml", "yarn.lock"),
    "cargo": ("Cargo.lock",),
    "go": ("go.sum",),
    "bundler": ("Gemfile.lock",),
    "composer": ("composer.lock",),
}


@dataclass(frozen=True, slots=True)
class DependencyChangePlan:
    operation: str
    manager: str
    package_name: str
    declaration: str
    development: bool
    manifest_name: str
    resolved: ResolvedPath
    opened: os.stat_result
    original: bytes
    updated: bytes

    @property
    def changed(self) -> bool:
        return self.original != self.updated

    @property
    def displayed_declaration(self) -> str:
        if self.operation == "remove" or self.manager == "pip":
            return self.declaration
        return f"{self.package_name}@{self.declaration}"


class DependencyMutationTools:
    """Change one manifest atomically while refusing code execution and stale locks."""

    def __init__(
        self,
        policy: PathPolicy,
        read_limits: ReadToolLimits,
        mutation_limits: MutationToolLimits,
    ) -> None:
        self.policy = policy
        self.read_limits = read_limits
        self.mutation_limits = mutation_limits
        self.io = SafeIO(policy)
        self.mutations = SafeMutationIO(policy)

    def confirmation_details(
        self,
        action: ValidatedAction,
    ) -> tuple[str | None, str]:
        plan = self._prepare(action)
        guard = self._guard(plan)
        if not plan.changed:
            return None, guard
        scope = "development" if plan.development else "runtime"
        verb = "add" if plan.operation == "install" else "remove"
        reason = (
            f"{action.spec.name} will {verb} the exact {plan.manager} {scope} "
            f"declaration {plan.displayed_declaration!r} in {plan.manifest_name!r}; "
            "this changes project dependency policy but downloads and package scripts "
            "stay disabled"
        )
        return reason, guard

    def install_dependency(self, action: ValidatedAction) -> Result:
        return self._apply(self._prepare(action), action)

    def remove_dependency(self, action: ValidatedAction) -> Result:
        return self._apply(self._prepare(action), action)

    def _apply(self, plan: DependencyChangePlan, action: ValidatedAction) -> Result:
        if plan.changed:
            executable = bool(plan.opened.st_mode & stat.S_IXUSR)
            self.mutations.atomic_replace(
                plan.resolved,
                plan.updated,
                executable=executable,
            )
            verb = "Added" if plan.operation == "install" else "Removed"
            summary = (
                f"{verb} {plan.displayed_declaration!r} in {plan.manifest_name!r} "
                f"({len(plan.updated)} bytes)."
            )
        else:
            summary = (
                f"{plan.displayed_declaration!r} is already declared exactly in "
                f"{plan.manifest_name!r}; no file changed."
            )
        return Result(
            action.source.id,
            ResultStatus.SUCCESS,
            summary
            + "\nnetwork=disabled\npackage_artifacts=not_installed\n"
            + "package_scripts=not_executed\nlockfile=absent",
        )

    def _prepare(self, action: ValidatedAction) -> DependencyChangePlan:
        manager = action.argument("manager")
        package = action.argument("package")
        development_value = action.argument("dev")
        if not isinstance(manager, str):
            raise ToolExecutionError("invalid_argument", "manager must be provided")
        if not isinstance(package, str) or not package:
            raise ToolExecutionError(
                "invalid_argument",
                "package must be an exact registry declaration",
            )
        development = development_value is True
        operation = "install" if action.spec.name == "install-dependency" else "remove"
        if manager == "go" and development:
            raise ToolExecutionError(
                "invalid_argument",
                "Go modules do not have a manifest-level development dependency scope",
            )

        package_name, declaration = self._parse_package(manager, package, operation)
        self._reject_lockfiles(manager)

        if manager == "pip":
            return self._prepare_pip(
                operation,
                package_name,
                declaration,
                development,
            )
        if manager in {"npm", "pnpm", "yarn"}:
            return self._prepare_javascript(
                manager,
                operation,
                package_name,
                declaration,
                development,
            )
        if manager == "cargo":
            return self._prepare_cargo(
                operation,
                package_name,
                declaration,
                development,
            )
        if manager == "go":
            return self._prepare_go(operation, package_name, declaration)
        if manager == "bundler":
            return self._prepare_bundler(
                operation,
                package_name,
                declaration,
                development,
            )
        if manager == "composer":
            return self._prepare_composer(
                operation,
                package_name,
                declaration,
                development,
            )
        raise ToolExecutionError("manager_unsupported", f"Unsupported manager {manager!r}")

    def _prepare_pip(
        self,
        operation: str,
        name: str,
        declaration: str,
        development: bool,
    ) -> DependencyChangePlan:
        if self._path_exists("pyproject.toml"):
            resolved, opened, original = self._load("pyproject.toml")
            updated = self._update_pep621(
                original,
                operation=operation,
                name=name,
                declaration=declaration,
                development=development,
            )
            return self._plan(
                operation,
                "pip",
                name,
                declaration,
                development,
                "pyproject.toml",
                resolved,
                opened,
                original,
                updated,
            )

        if operation == "install":
            manifest = "requirements-dev.txt" if development else "requirements.txt"
            resolved, opened, original = self._load(manifest)
        else:
            matches: list[str] = []
            for candidate in self._requirements_names():
                _, _, payload = self._load(candidate)
                if self._requirements_contains(payload, name):
                    matches.append(candidate)
            if not matches:
                raise ToolExecutionError(
                    "dependency_not_found",
                    f"pip dependency {name!r} is not declared",
                )
            if len(matches) > 1:
                raise ToolExecutionError(
                    "manifest_ambiguous",
                    f"pip dependency {name!r} appears in multiple requirements files",
                )
            manifest = matches[0]
            resolved, opened, original = self._load(manifest)

        updated = self._update_requirements(
            original,
            operation=operation,
            name=name,
            declaration=declaration,
            manifest=manifest,
        )
        return self._plan(
            operation,
            "pip",
            name,
            declaration,
            development,
            manifest,
            resolved,
            opened,
            original,
            updated,
        )

    def _prepare_javascript(
        self,
        manager: str,
        operation: str,
        name: str,
        declaration: str,
        development: bool,
    ) -> DependencyChangePlan:
        resolved, opened, original = self._load("package.json")
        data = DependencyReadTools._json(original, "package.json")
        sections = (
            "dependencies",
            "devDependencies",
            "peerDependencies",
            "optionalDependencies",
        )
        target = "devDependencies" if development else "dependencies"
        locations = []
        for section in sections:
            values = data.get(section)
            if values is None:
                continue
            if not isinstance(values, dict):
                raise ToolExecutionError(
                    "manifest_unsupported",
                    f"package.json field {section!r} must be an object",
                )
            if name in values:
                locations.append((section, values[name]))

        if operation == "install":
            if locations:
                if locations == [(target, declaration)]:
                    updated = original
                else:
                    raise ToolExecutionError(
                        "dependency_exists",
                        f"JavaScript dependency {name!r} is already declared",
                    )
            else:
                values = data.setdefault(target, {})
                assert isinstance(values, dict)
                values[name] = declaration
                updated = self._json_bytes(data)
        else:
            if not locations:
                raise ToolExecutionError(
                    "dependency_not_found",
                    f"JavaScript dependency {name!r} is not declared",
                )
            for section, _ in locations:
                values = data[section]
                assert isinstance(values, dict)
                del values[name]
                if not values:
                    del data[section]
            updated = self._json_bytes(data)

        return self._plan(
            operation,
            manager,
            name,
            declaration,
            development,
            "package.json",
            resolved,
            opened,
            original,
            updated,
        )

    def _prepare_composer(
        self,
        operation: str,
        name: str,
        declaration: str,
        development: bool,
    ) -> DependencyChangePlan:
        resolved, opened, original = self._load("composer.json")
        data = DependencyReadTools._json(original, "composer.json")
        sections = ("require", "require-dev")
        target = "require-dev" if development else "require"
        locations = []
        for section in sections:
            values = data.get(section)
            if values is None:
                continue
            if not isinstance(values, dict):
                raise ToolExecutionError(
                    "manifest_unsupported",
                    f"composer.json field {section!r} must be an object",
                )
            if name in values:
                locations.append((section, values[name]))

        if operation == "install":
            if locations:
                if locations == [(target, declaration)]:
                    updated = original
                else:
                    raise ToolExecutionError(
                        "dependency_exists",
                        f"Composer dependency {name!r} is already declared",
                    )
            else:
                values = data.setdefault(target, {})
                assert isinstance(values, dict)
                values[name] = declaration
                updated = self._json_bytes(data)
        else:
            if not locations:
                raise ToolExecutionError(
                    "dependency_not_found",
                    f"Composer dependency {name!r} is not declared",
                )
            for section, _ in locations:
                values = data[section]
                assert isinstance(values, dict)
                del values[name]
                if not values:
                    del data[section]
            updated = self._json_bytes(data)

        return self._plan(
            operation,
            "composer",
            name,
            declaration,
            development,
            "composer.json",
            resolved,
            opened,
            original,
            updated,
        )

    def _prepare_cargo(
        self,
        operation: str,
        name: str,
        declaration: str,
        development: bool,
    ) -> DependencyChangePlan:
        resolved, opened, original = self._load("Cargo.toml")
        text = self._utf8(original, "Cargo.toml")
        data = DependencyReadTools._toml(original, "Cargo.toml")
        sections = ("dependencies", "dev-dependencies", "build-dependencies")
        target = "dev-dependencies" if development else "dependencies"
        locations: list[tuple[str, Any]] = []
        for section in sections:
            values = data.get(section)
            if isinstance(values, Mapping) and name in values:
                locations.append((section, values[name]))

        exact_value = f"={declaration}"
        if operation == "install":
            if locations:
                if locations == [(target, exact_value)]:
                    updated_text = text
                else:
                    raise ToolExecutionError(
                        "dependency_exists",
                        f"Cargo dependency {name!r} is already declared",
                    )
            else:
                updated_text = self._toml_add_mapping(text, target, name, exact_value)
        else:
            if not locations:
                raise ToolExecutionError(
                    "dependency_not_found",
                    f"Cargo dependency {name!r} is not declared",
                )
            updated_text = text
            for section, _ in locations:
                updated_text = self._toml_remove_mapping(updated_text, section, name)

        updated = updated_text.encode("utf-8")
        DependencyReadTools._toml(updated, "Cargo.toml")
        return self._plan(
            operation,
            "cargo",
            name,
            declaration,
            development,
            "Cargo.toml",
            resolved,
            opened,
            original,
            updated,
        )

    def _prepare_go(
        self,
        operation: str,
        name: str,
        declaration: str,
    ) -> DependencyChangePlan:
        resolved, opened, original = self._load("go.mod")
        text = self._utf8(original, "go.mod")
        if not any(line.strip().startswith("module ") for line in text.splitlines()):
            raise ToolExecutionError("manifest_unsupported", "go.mod has no module directive")
        requirements = self._go_requirements(text)
        matches = [entry for entry in requirements if entry[1] == name]
        if operation == "install":
            if matches:
                if len(matches) == 1 and matches[0][2] == declaration:
                    updated_text = text
                else:
                    raise ToolExecutionError(
                        "dependency_exists",
                        f"Go dependency {name!r} is already declared",
                    )
            else:
                separator = "" if not text or text.endswith("\n") else "\n"
                updated_text = f"{text}{separator}require {name} {declaration}\n"
        else:
            if not matches:
                raise ToolExecutionError(
                    "dependency_not_found",
                    f"Go dependency {name!r} is not declared",
                )
            remove_indexes = {entry[0] for entry in matches}
            lines = text.splitlines(keepends=True)
            lines = [line for index, line in enumerate(lines) if index not in remove_indexes]
            updated_text = self._remove_empty_go_blocks("".join(lines))

        return self._plan(
            operation,
            "go",
            name,
            declaration,
            False,
            "go.mod",
            resolved,
            opened,
            original,
            updated_text.encode("utf-8"),
        )

    def _prepare_bundler(
        self,
        operation: str,
        name: str,
        declaration: str,
        development: bool,
    ) -> DependencyChangePlan:
        resolved, opened, original = self._load("Gemfile")
        text = self._utf8(original, "Gemfile")
        lines = text.splitlines(keepends=True)
        matches = [
            (index, match)
            for index, line in enumerate(lines)
            if (match := _GEM_DECLARATION.match(line.rstrip("\r\n")))
            and match.group("name") == name
        ]
        gem_line = f"gem {json.dumps(name)}, {json.dumps('= ' + declaration)}"
        if operation == "install":
            if matches:
                existing_version = matches[0][1].group("version")
                if len(matches) == 1 and existing_version == f"= {declaration}":
                    updated_text = text
                else:
                    raise ToolExecutionError(
                        "dependency_exists",
                        f"Bundler dependency {name!r} is already declared",
                    )
            else:
                separator = "" if not text or text.endswith("\n") else "\n"
                if development:
                    addition = f"group :development do\n  {gem_line}\nend\n"
                else:
                    addition = gem_line + "\n"
                updated_text = text + separator + addition
        else:
            if not matches:
                raise ToolExecutionError(
                    "dependency_not_found",
                    f"Bundler dependency {name!r} is not declared by a simple gem line",
                )
            if any(lines[index].rstrip("\r\n").rstrip().endswith(",") for index, _ in matches):
                raise ToolExecutionError(
                    "manifest_unsupported",
                    "A continued Gemfile declaration cannot be removed safely",
                )
            remove_indexes = {index for index, _ in matches}
            updated_text = "".join(
                line
                for index, line in enumerate(lines)
                if index not in remove_indexes
            )

        return self._plan(
            operation,
            "bundler",
            name,
            declaration,
            development,
            "Gemfile",
            resolved,
            opened,
            original,
            updated_text.encode("utf-8"),
        )

    def _update_pep621(
        self,
        payload: bytes,
        *,
        operation: str,
        name: str,
        declaration: str,
        development: bool,
    ) -> bytes:
        data = DependencyReadTools._toml(payload, "pyproject.toml")
        text = self._utf8(payload, "pyproject.toml")
        project = data.get("project")
        if not isinstance(project, Mapping):
            raise ToolExecutionError(
                "manifest_unsupported",
                "pip changes require a PEP 621 [project] table",
            )
        runtime = project.get("dependencies", [])
        optional = project.get("optional-dependencies", {})
        if not isinstance(runtime, list) or not all(isinstance(item, str) for item in runtime):
            raise ToolExecutionError(
                "manifest_unsupported",
                "[project].dependencies must be an array of strings",
            )
        if not isinstance(optional, Mapping):
            raise ToolExecutionError(
                "manifest_unsupported",
                "[project.optional-dependencies] must be a table",
            )
        groups: dict[str, list[str]] = {}
        for group, values in optional.items():
            if not isinstance(group, str) or not isinstance(values, list) or not all(
                isinstance(item, str) for item in values
            ):
                raise ToolExecutionError(
                    "manifest_unsupported",
                    "PEP 621 optional dependency groups must be string arrays",
                )
            groups[group] = list(values)

        locations: list[tuple[str, str]] = []
        for item in runtime:
            if self._pip_declaration_name(item) == name:
                locations.append(("runtime", item))
        for group, values in groups.items():
            for item in values:
                if self._pip_declaration_name(item) == name:
                    locations.append((group, item))

        target = "dev" if development else "runtime"
        if operation == "install":
            if locations:
                if locations == [(target, declaration)]:
                    updated_text = text
                else:
                    raise ToolExecutionError(
                        "dependency_exists",
                        f"pip dependency {name!r} is already declared",
                    )
            elif development:
                values = groups.get("dev", []) + [declaration]
                updated_text = self._toml_replace_array(
                    text,
                    "project.optional-dependencies",
                    "dev",
                    values,
                    create_table=True,
                )
            else:
                updated_text = self._toml_replace_array(
                    text,
                    "project",
                    "dependencies",
                    list(runtime) + [declaration],
                )
        else:
            if not locations:
                raise ToolExecutionError(
                    "dependency_not_found",
                    f"pip dependency {name!r} is not declared",
                )
            updated_text = text
            filtered_runtime = [
                item for item in runtime if self._pip_declaration_name(item) != name
            ]
            if len(filtered_runtime) != len(runtime):
                updated_text = self._toml_replace_array(
                    updated_text,
                    "project",
                    "dependencies",
                    filtered_runtime,
                )
            for group, values in groups.items():
                filtered = [
                    item for item in values if self._pip_declaration_name(item) != name
                ]
                if len(filtered) != len(values):
                    updated_text = self._toml_replace_array(
                        updated_text,
                        "project.optional-dependencies",
                        group,
                        filtered,
                    )

        updated = updated_text.encode("utf-8")
        DependencyReadTools._toml(updated, "pyproject.toml")
        return updated

    def _update_requirements(
        self,
        payload: bytes,
        *,
        operation: str,
        name: str,
        declaration: str,
        manifest: str,
    ) -> bytes:
        text = self._utf8(payload, manifest)
        lines = text.splitlines(keepends=True)
        matches = [
            index
            for index, line in enumerate(lines)
            if self._pip_declaration_name(self._requirement_value(line)) == name
        ]
        if operation == "install":
            if matches:
                values = [self._requirement_value(lines[index]) for index in matches]
                if len(values) == 1 and values[0] == declaration:
                    return payload
                raise ToolExecutionError(
                    "dependency_exists",
                    f"pip dependency {name!r} is already declared",
                )
            separator = b"" if not payload or payload.endswith(b"\n") else b"\n"
            return payload + separator + declaration.encode("utf-8") + b"\n"
        if not matches:
            raise ToolExecutionError(
                "dependency_not_found",
                f"pip dependency {name!r} is not declared",
            )
        if any(lines[index].rstrip("\r\n").rstrip().endswith("\\") for index in matches):
            raise ToolExecutionError(
                "manifest_unsupported",
                "A continued requirements declaration cannot be removed safely",
            )
        return "".join(
            line for index, line in enumerate(lines) if index not in set(matches)
        ).encode("utf-8")

    def _parse_package(
        self,
        manager: str,
        value: str,
        operation: str,
    ) -> tuple[str, str]:
        if len(value.encode("utf-8")) > 512 or any(
            ord(character) < 32 or ord(character) == 127 for character in value
        ):
            raise ToolExecutionError("invalid_dependency", "Dependency text is invalid")
        if value != value.strip():
            raise ToolExecutionError(
                "invalid_dependency",
                "Dependency text cannot have whitespace",
            )

        if manager == "pip":
            if operation == "remove":
                if not _PIP_NAME.fullmatch(value):
                    raise self._exact_error(manager, operation)
                return self._canonical_pip(value), value
            match = _PIP_INSTALL.fullmatch(value)
            if match is None:
                raise self._exact_error(manager, operation)
            name = self._canonical_pip(match.group("name"))
            return name, value

        if manager in {"npm", "pnpm", "yarn"}:
            if operation == "remove":
                if not _NPM_NAME.fullmatch(value):
                    raise self._exact_error(manager, operation)
                return value, value
            name, version = self._split_at_version(value, scoped=value.startswith("@"))
            if not _NPM_NAME.fullmatch(name) or not _SEMVER.fullmatch(version):
                raise self._exact_error(manager, operation)
            return name, version

        if manager in {"cargo", "bundler"}:
            if operation == "remove":
                if not _PLAIN_PACKAGE.fullmatch(value):
                    raise self._exact_error(manager, operation)
                return value, value
            name, version = self._split_at_version(value)
            if not _PLAIN_PACKAGE.fullmatch(name) or not _SEMVER.fullmatch(version):
                raise self._exact_error(manager, operation)
            return name, version

        if manager == "go":
            if operation == "remove":
                if not _GO_MODULE.fullmatch(value):
                    raise self._exact_error(manager, operation)
                return value, value
            name, version = self._split_at_version(value)
            if not _GO_MODULE.fullmatch(name) or not _GO_VERSION.fullmatch(version):
                raise self._exact_error(manager, operation)
            return name, version

        if manager == "composer":
            if operation == "remove":
                if not _COMPOSER_NAME.fullmatch(value):
                    raise self._exact_error(manager, operation)
                return value, value
            name, version = self._split_at_version(value)
            normalized = version[1:] if version.startswith("v") else version
            if not _COMPOSER_NAME.fullmatch(name) or not _SEMVER.fullmatch(normalized):
                raise self._exact_error(manager, operation)
            return name, version

        raise ToolExecutionError("manager_unsupported", f"Unsupported manager {manager!r}")

    @staticmethod
    def _exact_error(manager: str, operation: str) -> ToolExecutionError:
        if operation == "remove":
            expected = "a registry package name without a version"
        elif manager == "pip":
            expected = "name==exact_version"
        elif manager == "go":
            expected = "module/path@vX.Y.Z"
        else:
            expected = "name@X.Y.Z"
        return ToolExecutionError(
            "invalid_dependency",
            (
                f"{manager} {operation} requires {expected}; URLs, paths, ranges, "
                "and tags are disabled"
            ),
        )

    @staticmethod
    def _split_at_version(value: str, *, scoped: bool = False) -> tuple[str, str]:
        name, separator, version = value.rpartition("@")
        if not separator or not name or not version:
            return "", ""
        if scoped and "/" not in name:
            return "", ""
        return name, version

    def _reject_lockfiles(self, manager: str) -> None:
        for name in _LOCKFILES[manager]:
            if self._path_exists(name):
                raise ToolExecutionError(
                    "lockfile_present",
                    f"{name!r} exists; this offline phase refuses to leave a stale lockfile",
                )

    def _load(self, name: str) -> tuple[ResolvedPath, os.stat_result, bytes]:
        try:
            resolved = self.policy.resolve(
                PathRef(name, Root.OUTPUT),
                access=PathAccess.WRITE,
                existence=PathExistence.MUST_EXIST,
                kind=PathKind.FILE,
            )
        except PathPolicyError as error:
            code = "manifest_not_found" if error.code == "path_not_found" else error.code
            raise ToolExecutionError(
                code,
                f"Dependency manifest {name!r} is unavailable",
            ) from error
        limit = min(self.read_limits.max_manifest_bytes, self.mutation_limits.max_file_bytes)
        with self.io.open_file(resolved) as stream:
            opened = os.fstat(stream.fileno())
            payload = stream.read(limit + 1)
        if len(payload) > limit:
            raise ToolExecutionError(
                "manifest_too_large",
                f"Dependency manifest {name!r} exceeds {limit} bytes",
            )
        if b"\x00" in payload:
            raise ToolExecutionError(
                "binary_unsupported",
                f"Dependency manifest {name!r} is binary",
            )
        return resolved, opened, payload

    def _path_exists(self, name: str) -> bool:
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

    def _requirements_names(self) -> list[str]:
        try:
            resolved = self.policy.resolve(
                PathRef(".", Root.OUTPUT),
                access=PathAccess.READ,
                existence=PathExistence.MUST_EXIST,
                kind=PathKind.DIRECTORY,
            )
        except PathPolicyError as error:
            raise ToolExecutionError(error.code, str(error)) from error
        names: list[str] = []
        with self.io.open_directory(resolved) as directory_fd:
            for entry in self.io.entries(directory_fd):
                if entry.is_file and _REQUIREMENTS_FILE.fullmatch(entry.name):
                    names.append(entry.name)
        return sorted(names, key=str.casefold)

    def _requirements_contains(self, payload: bytes, name: str) -> bool:
        text = self._utf8(payload, "requirements file")
        return any(
            self._pip_declaration_name(self._requirement_value(line)) == name
            for line in text.splitlines()
        )

    @staticmethod
    def _requirement_value(line: str) -> str:
        value = line.strip()
        if not value or value.startswith(("#", "-")):
            return ""
        return value.split(" #", 1)[0].strip()

    @classmethod
    def _pip_declaration_name(cls, value: str) -> str | None:
        match = _PIP_DECLARATION_NAME.match(value)
        return cls._canonical_pip(match.group("name")) if match else None

    @staticmethod
    def _canonical_pip(value: str) -> str:
        return re.sub(r"[-_.]+", "-", value).lower()

    @staticmethod
    def _json_bytes(data: dict[str, Any]) -> bytes:
        try:
            return (json.dumps(data, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
        except (TypeError, ValueError, RecursionError) as error:
            raise ToolExecutionError(
                "manifest_unsupported",
                "JSON manifest cannot be rewritten",
            ) from error

    @classmethod
    def _toml_replace_array(
        cls,
        text: str,
        table: str,
        key: str,
        values: list[str],
        *,
        create_table: bool = False,
    ) -> str:
        span = cls._toml_table_span(text, table)
        rendered = cls._render_toml_array(values)
        if span is None:
            if not create_table:
                raise ToolExecutionError(
                    "manifest_unsupported",
                    f"TOML table [{table}] cannot be located safely",
                )
            separator = "" if not text or text.endswith("\n") else "\n"
            return f"{text}{separator}\n[{table}]\n{key} = {rendered}\n"

        start, end = span
        key_pattern = cls._toml_key_pattern(key)
        match = re.search(rf"(?m)^[ \t]*(?:{key_pattern})[ \t]*=", text[start:end])
        if match is None:
            insertion = end
            prefix = "" if insertion == 0 or text[insertion - 1] == "\n" else "\n"
            return text[:insertion] + f"{prefix}{key} = {rendered}\n" + text[insertion:]

        absolute_end = start + match.end()
        value_start = absolute_end
        while value_start < len(text) and text[value_start] in " \t":
            value_start += 1
        if value_start >= len(text) or text[value_start] != "[":
            raise ToolExecutionError(
                "manifest_unsupported",
                f"TOML key {key!r} is not a literal array",
            )
        value_end = cls._toml_array_end(text, value_start)
        return text[:value_start] + rendered + text[value_end:]

    @classmethod
    def _toml_add_mapping(cls, text: str, table: str, key: str, value: str) -> str:
        span = cls._toml_table_span(text, table)
        line = f"{json.dumps(key, ensure_ascii=False)} = {json.dumps(value)}\n"
        if span is None:
            separator = "" if not text or text.endswith("\n") else "\n"
            return f"{text}{separator}\n[{table}]\n{line}"
        _, end = span
        prefix = "" if end == 0 or text[end - 1] == "\n" else "\n"
        return text[:end] + prefix + line + text[end:]

    @classmethod
    def _toml_remove_mapping(cls, text: str, table: str, key: str) -> str:
        span = cls._toml_table_span(text, table)
        if span is None:
            raise ToolExecutionError(
                "manifest_unsupported",
                f"TOML table [{table}] cannot be located safely",
            )
        start, end = span
        pattern = re.compile(
            rf"(?m)^[ \t]*(?:{cls._toml_key_pattern(key)})[ \t]*=.*(?:\n|\Z)"
        )
        matches = list(pattern.finditer(text[start:end]))
        if len(matches) != 1:
            raise ToolExecutionError(
                "manifest_unsupported",
                f"TOML dependency {key!r} cannot be located as one assignment",
            )
        match = matches[0]
        return text[: start + match.start()] + text[start + match.end() :]

    @staticmethod
    def _toml_key_pattern(key: str) -> str:
        bare = re.escape(key) if re.fullmatch(r"[A-Za-z0-9_-]+", key) else r"(?!)"
        quoted = re.escape(json.dumps(key, ensure_ascii=False))
        return f"(?:{bare}|{quoted})"

    @staticmethod
    def _toml_table_span(text: str, table: str) -> tuple[int, int] | None:
        wanted = f"[{table}]"
        headers = list(_TOML_HEADER.finditer(text))
        selected = None
        for index, header in enumerate(headers):
            value = header.group(0).split("#", 1)[0].strip()
            if value == wanted:
                if selected is not None:
                    raise ToolExecutionError(
                        "manifest_unsupported",
                        f"TOML table {wanted} appears more than once",
                    )
                selected = index
        if selected is None:
            return None
        start = headers[selected].end()
        end = headers[selected + 1].start() if selected + 1 < len(headers) else len(text)
        return start, end

    @staticmethod
    def _render_toml_array(values: list[str]) -> str:
        if not values:
            return "[]"
        rendered = ",\n".join(
            "    " + json.dumps(value, ensure_ascii=False) for value in values
        )
        return f"[\n{rendered},\n]"

    @staticmethod
    def _toml_array_end(text: str, start: int) -> int:
        depth = 0
        quote: str | None = None
        escaped = False
        index = start
        while index < len(text):
            character = text[index]
            if quote is not None:
                if quote == '"' and escaped:
                    escaped = False
                elif quote == '"' and character == "\\":
                    escaped = True
                elif character == quote:
                    quote = None
            elif character in {'"', "'"}:
                quote = character
            elif character == "#":
                newline = text.find("\n", index)
                if newline < 0:
                    break
                index = newline
            elif character == "[":
                depth += 1
            elif character == "]":
                depth -= 1
                if depth == 0:
                    return index + 1
            index += 1
        raise ToolExecutionError("manifest_unsupported", "TOML array is not safely bounded")

    @staticmethod
    def _go_requirements(text: str) -> list[tuple[int, str, str]]:
        output: list[tuple[int, str, str]] = []
        in_block = False
        for index, raw_line in enumerate(text.splitlines(keepends=True)):
            line = raw_line.split("//", 1)[0].strip()
            if line == "require (":
                in_block = True
                continue
            if in_block and line == ")":
                in_block = False
                continue
            if line.startswith("require "):
                line = line[len("require ") :].strip()
            elif not in_block:
                continue
            fields = line.split()
            if len(fields) >= 2:
                output.append((index, fields[0], fields[1]))
        return output

    @staticmethod
    def _remove_empty_go_blocks(text: str) -> str:
        lines = text.splitlines(keepends=True)
        output: list[str] = []
        index = 0
        while index < len(lines):
            if lines[index].strip() != "require (":
                output.append(lines[index])
                index += 1
                continue
            end = index + 1
            meaningful = False
            while end < len(lines) and lines[end].strip() != ")":
                meaningful = meaningful or bool(lines[end].strip())
                end += 1
            if end < len(lines) and not meaningful:
                index = end + 1
                continue
            output.append(lines[index])
            index += 1
        return "".join(output)

    def _plan(
        self,
        operation: str,
        manager: str,
        package_name: str,
        declaration: str,
        development: bool,
        manifest_name: str,
        resolved: ResolvedPath,
        opened: os.stat_result,
        original: bytes,
        updated: bytes,
    ) -> DependencyChangePlan:
        if len(updated) > self.mutation_limits.max_file_bytes:
            raise ToolExecutionError(
                "manifest_too_large",
                f"Updated dependency manifest exceeds {self.mutation_limits.max_file_bytes} bytes",
            )
        return DependencyChangePlan(
            operation,
            manager,
            package_name,
            declaration,
            development,
            manifest_name,
            resolved,
            opened,
            original,
            updated,
        )

    @staticmethod
    def _guard(plan: DependencyChangePlan) -> str:
        payload = json.dumps(
            {
                "manifest": plan.manifest_name,
                "device": plan.opened.st_dev,
                "inode": plan.opened.st_ino,
                "mode": plan.opened.st_mode,
                "size": len(plan.original),
                "mtime_ns": plan.opened.st_mtime_ns,
                "ctime_ns": plan.opened.st_ctime_ns,
                "sha256": hashlib.sha256(plan.original).hexdigest(),
            },
            separators=(",", ":"),
            sort_keys=True,
        ).encode("ascii")
        return hashlib.sha256(payload).hexdigest()

    @staticmethod
    def _utf8(payload: bytes, name: str) -> str:
        try:
            return payload.decode("utf-8")
        except UnicodeDecodeError as error:
            raise ToolExecutionError("binary_unsupported", f"{name} is not UTF-8 text") from error
