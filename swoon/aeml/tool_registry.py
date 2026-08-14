"""Declarative schemas for every tool named by AEML v0.3."""

from __future__ import annotations

from types import MappingProxyType

from .models import (
    ArgumentKind,
    ArgumentSpec,
    Confirmation,
    Root,
    ToolEffect,
    ToolSpec,
)


READ_ROOTS = frozenset({Root.INPUT, Root.OUTPUT})
OUTPUT_ROOT = frozenset({Root.OUTPUT})
PACKAGE_MANAGERS = ("pip", "npm", "pnpm", "yarn", "cargo", "go", "bundler", "composer")


def text_arg(name: str, *, required: bool = False, allow_empty: bool = False) -> ArgumentSpec:
    return ArgumentSpec(name, required=required, allow_empty=allow_empty)


def int_arg(
    name: str,
    *,
    required: bool = False,
    minimum: int | None = None,
    maximum: int | None = None,
) -> ArgumentSpec:
    return ArgumentSpec(
        name,
        kind=ArgumentKind.INTEGER,
        required=required,
        minimum=minimum,
        maximum=maximum,
    )


def bool_arg(name: str, *, required: bool = False) -> ArgumentSpec:
    return ArgumentSpec(name, kind=ArgumentKind.BOOLEAN, required=required)


def enum_arg(name: str, choices: tuple[str, ...], *, required: bool = False) -> ArgumentSpec:
    return ArgumentSpec(name, kind=ArgumentKind.ENUM, required=required, choices=choices)


def path_arg(
    name: str,
    roots: frozenset[Root],
    *,
    required: bool = False,
    write_target: bool = False,
) -> ArgumentSpec:
    return ArgumentSpec(
        name,
        kind=ArgumentKind.PATH,
        required=required,
        allowed_roots=roots,
        write_target=write_target,
    )


TIMEOUT = int_arg("timeout", minimum=1, maximum=3_600)
MAX_OUTPUT_LINES = int_arg("max_output_lines", minimum=1, maximum=100_000)
MANAGER = enum_arg("manager", PACKAGE_MANAGERS)


_SPECS = (
    ToolSpec(
        "run-command",
        ToolEffect.EXECUTING,
        (text_arg("cmd", required=True), TIMEOUT, MAX_OUTPUT_LINES),
    ),
    ToolSpec(
        "run-command-background",
        ToolEffect.EXECUTING,
        (text_arg("cmd", required=True), MAX_OUTPUT_LINES),
    ),
    ToolSpec("kill-process", ToolEffect.EXECUTING, (text_arg("handle", required=True),)),
    ToolSpec(
        "stream-output",
        ToolEffect.READ_ONLY,
        (
            text_arg("handle", required=True),
            int_arg("offset", minimum=0),
            MAX_OUTPUT_LINES,
        ),
    ),
    ToolSpec("get-env", ToolEffect.READ_ONLY, (text_arg("name"),)),
    ToolSpec(
        "set-env",
        ToolEffect.MUTATING,
        (text_arg("name", required=True), text_arg("value", required=True, allow_empty=True)),
    ),
    ToolSpec(
        "read-file",
        ToolEffect.READ_ONLY,
        (int_arg("start_line", minimum=1), int_arg("end_line", minimum=1)),
        path_required=True,
        path_allowed=True,
        path_roots=READ_ROOTS,
    ),
    ToolSpec(
        "list-dir",
        ToolEffect.READ_ONLY,
        (bool_arg("recursive"), text_arg("pattern")),
        path_required=True,
        path_allowed=True,
        path_roots=READ_ROOTS,
    ),
    ToolSpec(
        "grep",
        ToolEffect.READ_ONLY,
        (
            text_arg("pattern", required=True),
            int_arg("max_results", minimum=1, maximum=100_000),
            int_arg("context_lines", minimum=0, maximum=1_000),
        ),
        path_required=True,
        path_allowed=True,
        path_roots=READ_ROOTS,
    ),
    ToolSpec(
        "create-file",
        ToolEffect.MUTATING,
        (text_arg("content", required=True, allow_empty=True),),
        path_required=True,
        path_allowed=True,
        path_roots=OUTPUT_ROOT,
        path_write_target=True,
        supports_chunk=True,
    ),
    ToolSpec(
        "overwrite-file",
        ToolEffect.MUTATING,
        (text_arg("content", required=True, allow_empty=True),),
        path_required=True,
        path_allowed=True,
        path_roots=OUTPUT_ROOT,
        path_write_target=True,
        supports_chunk=True,
        confirmation=Confirmation.CONDITIONAL,
    ),
    ToolSpec(
        "append-file",
        ToolEffect.MUTATING,
        (text_arg("content", required=True, allow_empty=True),),
        path_required=True,
        path_allowed=True,
        path_roots=OUTPUT_ROOT,
        path_write_target=True,
        supports_chunk=True,
    ),
    ToolSpec(
        "edit-file",
        ToolEffect.MUTATING,
        (text_arg("old_str", required=True), text_arg("new_str", required=True, allow_empty=True)),
        path_required=True,
        path_allowed=True,
        path_roots=OUTPUT_ROOT,
        path_write_target=True,
    ),
    ToolSpec(
        "copy-file",
        ToolEffect.MUTATING,
        (
            path_arg("from", READ_ROOTS, required=True),
            path_arg("to", OUTPUT_ROOT, required=True, write_target=True),
        ),
    ),
    ToolSpec(
        "copy-dir",
        ToolEffect.MUTATING,
        (
            path_arg("from", READ_ROOTS, required=True),
            path_arg("to", OUTPUT_ROOT, required=True, write_target=True),
        ),
    ),
    ToolSpec(
        "delete-file",
        ToolEffect.MUTATING,
        path_required=True,
        path_allowed=True,
        path_roots=OUTPUT_ROOT,
        path_write_target=True,
        confirmation=Confirmation.ALWAYS,
    ),
    ToolSpec(
        "delete-dir",
        ToolEffect.MUTATING,
        path_required=True,
        path_allowed=True,
        path_roots=OUTPUT_ROOT,
        path_write_target=True,
        confirmation=Confirmation.ALWAYS,
    ),
    ToolSpec(
        "move",
        ToolEffect.MUTATING,
        (
            path_arg("from", OUTPUT_ROOT, required=True, write_target=True),
            path_arg("to", OUTPUT_ROOT, required=True, write_target=True),
        ),
    ),
    ToolSpec(
        "rename",
        ToolEffect.MUTATING,
        (
            path_arg("from", OUTPUT_ROOT, required=True, write_target=True),
            path_arg("to", OUTPUT_ROOT, required=True, write_target=True),
        ),
    ),
    ToolSpec(
        "chmod",
        ToolEffect.MUTATING,
        (text_arg("mode", required=True),),
        path_required=True,
        path_allowed=True,
        path_roots=OUTPUT_ROOT,
        path_write_target=True,
    ),
    ToolSpec(
        "install-dependency",
        ToolEffect.EXECUTING,
        (enum_arg("manager", PACKAGE_MANAGERS, required=True), text_arg("package"), bool_arg("dev")),
    ),
    ToolSpec(
        "remove-dependency",
        ToolEffect.EXECUTING,
        (enum_arg("manager", PACKAGE_MANAGERS, required=True), text_arg("package", required=True)),
    ),
    ToolSpec("list-dependencies", ToolEffect.READ_ONLY, (MANAGER,)),
    ToolSpec("git-init", ToolEffect.MUTATING),
    ToolSpec("git-status", ToolEffect.READ_ONLY),
    ToolSpec(
        "git-diff",
        ToolEffect.READ_ONLY,
        (bool_arg("staged"), MAX_OUTPUT_LINES),
        path_allowed=True,
        path_roots=OUTPUT_ROOT,
    ),
    ToolSpec("git-log", ToolEffect.READ_ONLY, (int_arg("max_count", minimum=1, maximum=1_000),)),
    ToolSpec(
        "git-add",
        ToolEffect.MUTATING,
        path_required=True,
        path_allowed=True,
        path_roots=OUTPUT_ROOT,
        path_write_target=True,
    ),
    ToolSpec("git-commit", ToolEffect.MUTATING, (text_arg("message", required=True),)),
    ToolSpec(
        "git-branch",
        ToolEffect.MUTATING,
        (text_arg("name", required=True), text_arg("start_point")),
    ),
    ToolSpec("git-checkout", ToolEffect.MUTATING, (text_arg("ref", required=True),)),
    ToolSpec(
        "git-push",
        ToolEffect.EXECUTING,
        (text_arg("remote"), text_arg("branch")),
    ),
    ToolSpec(
        "git-pull",
        ToolEffect.EXECUTING,
        (text_arg("remote"), text_arg("branch")),
    ),
    ToolSpec(
        "git-merge",
        ToolEffect.MUTATING,
        (text_arg("ref", required=True),),
        confirmation=Confirmation.ALWAYS,
    ),
    ToolSpec(
        "git-rebase",
        ToolEffect.MUTATING,
        (text_arg("ref", required=True),),
        confirmation=Confirmation.ALWAYS,
    ),
    ToolSpec(
        "run-build",
        ToolEffect.EXECUTING,
        (MANAGER, text_arg("target"), TIMEOUT, MAX_OUTPUT_LINES),
    ),
    ToolSpec(
        "run-tests",
        ToolEffect.EXECUTING,
        (MANAGER, text_arg("target"), TIMEOUT, MAX_OUTPUT_LINES),
    ),
    ToolSpec(
        "run-linter",
        ToolEffect.EXECUTING,
        (MANAGER, text_arg("target"), TIMEOUT, MAX_OUTPUT_LINES),
    ),
)


TOOL_SPECS = MappingProxyType({spec.name: spec for spec in _SPECS})


def get_tool_spec(name: str) -> ToolSpec | None:
    return TOOL_SPECS.get(name)


def tool_names() -> tuple[str, ...]:
    return tuple(TOOL_SPECS)
