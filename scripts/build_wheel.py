#!/usr/bin/env python3
"""Build Swoon Code's pure-Python wheel without third-party build tooling."""

from __future__ import annotations

import argparse
import ast
import base64
import csv
import hashlib
import io
import os
import re
import tempfile
import tomllib
import zipfile
from pathlib import Path
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parents[1]
WHEEL_TIMESTAMP = (1980, 1, 1, 0, 0, 0)


def build_wheel(project_root: Path, output_directory: Path) -> Path:
    """Build and return one deterministic ``py3-none-any`` wheel."""

    root = project_root.resolve(strict=True)
    configuration = _project_configuration(root)
    project = configuration["project"]
    name = _required_text(project, "name")
    version = project_version(root)

    distribution = _normalize_distribution(name)
    wheel_version = _normalize_version(version)
    dist_info = f"{distribution}-{wheel_version}.dist-info"
    filename = f"{distribution}-{wheel_version}-py3-none-any.whl"
    output = output_directory.resolve()
    output.mkdir(mode=0o755, parents=True, exist_ok=True)
    if not output.is_dir():
        raise ValueError("Wheel output path is not a directory")

    payloads: dict[str, bytes] = {}
    for source in _package_files(root / "swoon"):
        relative = source.relative_to(root).as_posix()
        payloads[relative] = source.read_bytes()
    license_files = _license_files(project, root)
    for relative, payload in license_files:
        payloads[f"{dist_info}/licenses/{relative}"] = payload
    payloads[f"{dist_info}/METADATA"] = _metadata(
        project,
        root,
        tuple(relative for relative, _ in license_files),
    )
    payloads[f"{dist_info}/WHEEL"] = (
        "Wheel-Version: 1.0\n"
        "Generator: swoon-offline-wheel-builder 1\n"
        "Root-Is-Purelib: true\n"
        "Tag: py3-none-any\n"
    ).encode("utf-8")
    payloads[f"{dist_info}/entry_points.txt"] = _entry_points(project)
    payloads[f"{dist_info}/top_level.txt"] = b"swoon\n"

    record_name = f"{dist_info}/RECORD"
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{filename}.",
        suffix=".tmp",
        dir=output,
    )
    os.close(descriptor)
    temporary = Path(temporary_name)
    target = output / filename
    try:
        records: list[tuple[str, str, str]] = []
        with zipfile.ZipFile(
            temporary,
            mode="w",
            compression=zipfile.ZIP_DEFLATED,
            compresslevel=9,
        ) as archive:
            for archive_name in sorted(payloads):
                payload = payloads[archive_name]
                _write_member(archive, archive_name, payload)
                digest = base64.urlsafe_b64encode(hashlib.sha256(payload).digest()).rstrip(b"=")
                records.append(
                    (archive_name, f"sha256={digest.decode('ascii')}", str(len(payload)))
                )
            record = io.StringIO(newline="")
            writer = csv.writer(record, lineterminator="\n")
            writer.writerows(records)
            writer.writerow((record_name, "", ""))
            _write_member(archive, record_name, record.getvalue().encode("utf-8"))
        temporary.chmod(0o644)
        os.replace(temporary, target)
    finally:
        temporary.unlink(missing_ok=True)
    return target


def _project_configuration(root: Path) -> dict[str, Any]:
    raw = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))
    project = raw.get("project")
    if not isinstance(project, dict):
        raise ValueError("pyproject.toml is missing [project]")
    return {"project": project}


def project_version(project_root: Path) -> str:
    root = project_root.resolve(strict=True)
    project = _project_configuration(root)["project"]
    version = _required_text(project, "version")
    runtime_version = _runtime_version(root / "swoon" / "__init__.py")
    if runtime_version != version:
        raise ValueError(
            f"pyproject version {version!r} differs from runtime version {runtime_version!r}"
        )
    return version


def _runtime_version(path: Path) -> str:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    for node in tree.body:
        if not isinstance(node, ast.Assign) or len(node.targets) != 1:
            continue
        target = node.targets[0]
        if (
            isinstance(target, ast.Name)
            and target.id == "__version__"
            and isinstance(node.value, ast.Constant)
            and isinstance(node.value.value, str)
        ):
            return node.value.value
    raise ValueError("swoon.__version__ must be one literal assignment")


def _package_files(package_root: Path) -> tuple[Path, ...]:
    if not (package_root / "__init__.py").is_file():
        raise ValueError("swoon package is missing")
    files: list[Path] = []
    for path in package_root.rglob("*"):
        if path.is_symlink():
            raise ValueError("Package source cannot contain symbolic links")
        if not path.is_file() or "__pycache__" in path.parts or path.suffix == ".pyc":
            continue
        if path.suffix != ".py" and path.name != "py.typed":
            raise ValueError(f"Unrecognized package data file: {path.relative_to(package_root)}")
        files.append(path)
    return tuple(sorted(files))


def _metadata(
    project: dict[str, Any],
    root: Path,
    license_files: tuple[str, ...],
) -> bytes:
    name = _required_text(project, "name")
    version = _required_text(project, "version")
    description = _required_text(project, "description")
    requires_python = _required_text(project, "requires-python")
    readme_name = _required_text(project, "readme")
    readme = _project_file(root, readme_name).read_text(encoding="utf-8")
    license_expression = _required_text(project, "license")
    if not re.fullmatch(
        r"[A-Za-z0-9][A-Za-z0-9.+-]*(?:\s+(?:AND|OR|WITH)\s+"
        r"[A-Za-z0-9][A-Za-z0-9.+-]*)*",
        license_expression,
    ):
        raise ValueError("project license must be a simple SPDX expression")
    if not license_files:
        raise ValueError("project must include at least one license file")
    dependencies = project.get("dependencies", [])
    if not isinstance(dependencies, list) or not all(
        isinstance(item, str)
        and item.strip()
        and "\n" not in item
        and "\r" not in item
        for item in dependencies
    ):
        raise ValueError("project dependencies must be non-empty strings")
    lines = [
        "Metadata-Version: 2.4",
        f"Name: {name}",
        f"Version: {version}",
        f"Summary: {description}",
        f"Requires-Python: {requires_python}",
        f"License-Expression: {license_expression}",
        "Description-Content-Type: text/markdown",
    ]
    authors = project.get("authors", [])
    if not isinstance(authors, list) or not authors:
        raise ValueError("project authors must be a non-empty array")
    author_names: list[str] = []
    for author in authors:
        if not isinstance(author, dict) or set(author) != {"name"}:
            raise ValueError("offline builder requires authors with a name only")
        author_names.append(_required_text(author, "name"))
    lines.append(f"Author: {', '.join(author_names)}")

    keywords = project.get("keywords", [])
    if not isinstance(keywords, list) or not keywords:
        raise ValueError("project keywords must be a non-empty array")
    normalized_keywords: list[str] = []
    for keyword in keywords:
        if not isinstance(keyword, str):
            raise ValueError("project keywords must contain text")
        normalized_keywords.append(_single_line(keyword, "project keyword"))
    lines.append(f"Keywords: {','.join(normalized_keywords)}")

    classifiers = project.get("classifiers", [])
    if not isinstance(classifiers, list) or not classifiers:
        raise ValueError("project classifiers must be a non-empty array")
    for classifier in classifiers:
        if not isinstance(classifier, str):
            raise ValueError("project classifiers must contain text")
        lines.append(f"Classifier: {_single_line(classifier, 'project classifier')}")

    urls = project.get("urls", {})
    if not isinstance(urls, dict) or not urls:
        raise ValueError("project URLs must be a non-empty table")
    for label, url in sorted(urls.items()):
        if not isinstance(label, str) or not isinstance(url, str):
            raise ValueError("project URLs must map text labels to text URLs")
        lines.append(
            f"Project-URL: {_single_line(label, 'project URL label')}, "
            f"{_single_line(url, 'project URL')}"
        )
    lines.extend(f"License-File: {name}" for name in license_files)
    lines.extend(f"Requires-Dist: {dependency}" for dependency in dependencies)
    return ("\n".join(lines) + "\n\n" + readme).encode("utf-8")


def _license_files(
    project: dict[str, Any],
    root: Path,
) -> tuple[tuple[str, bytes], ...]:
    declared = project.get("license-files")
    if not isinstance(declared, list) or not declared:
        raise ValueError("project license-files must be a non-empty array")

    selected: list[tuple[str, bytes]] = []
    seen: set[str] = set()
    for value in declared:
        if (
            not isinstance(value, str)
            or not value
            or value != value.strip()
            or "\n" in value
            or "\r" in value
            or "\\" in value
            or any(character in value for character in "*?[]")
        ):
            raise ValueError(
                "offline wheel builder requires literal POSIX license-file paths"
            )
        source = _project_file(root, value)
        relative = source.relative_to(root).as_posix()
        if relative in seen:
            raise ValueError("project license-files contains a duplicate path")
        try:
            payload = source.read_bytes()
            payload.decode("utf-8")
        except UnicodeDecodeError as error:
            raise ValueError("Project license file must be UTF-8 text") from error
        seen.add(relative)
        selected.append((relative, payload))
    return tuple(selected)


def _entry_points(project: dict[str, Any]) -> bytes:
    scripts = project.get("scripts")
    if not isinstance(scripts, dict) or not scripts:
        raise ValueError("project must declare at least one console script")
    lines = ["[console_scripts]"]
    for name, target in sorted(scripts.items()):
        if (
            not isinstance(name, str)
            or not re.fullmatch(r"[A-Za-z0-9_.-]+", name)
            or not isinstance(target, str)
            or not target.strip()
            or "\n" in target
            or "\r" in target
        ):
            raise ValueError("console scripts must map text names to import targets")
        lines.append(f"{name} = {target}")
    return ("\n".join(lines) + "\n").encode("utf-8")


def _required_text(mapping: dict[str, Any], name: str) -> str:
    value = mapping.get(name)
    if (
        not isinstance(value, str)
        or not value.strip()
        or "\n" in value
        or "\r" in value
    ):
        raise ValueError(f"project {name!r} must be one non-empty line")
    return value.strip()


def _single_line(value: str, label: str) -> str:
    selected = value.strip()
    if not selected or "\n" in selected or "\r" in selected:
        raise ValueError(f"{label} must be one non-empty line")
    return selected


def _normalize_distribution(name: str) -> str:
    normalized = re.sub(r"[-_.]+", "_", name).lower()
    if not re.fullmatch(r"[a-z0-9_]+", normalized):
        raise ValueError("project name cannot form a safe wheel filename")
    return normalized


def _normalize_version(version: str) -> str:
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._+!-]*", version):
        raise ValueError("project version cannot form a safe wheel filename")
    return version.replace("-", "_")


def _project_file(root: Path, relative_name: str) -> Path:
    candidate = root / relative_name
    if candidate.is_symlink():
        raise ValueError("Project metadata file cannot be a symbolic link")
    try:
        selected = candidate.resolve(strict=True)
        selected.relative_to(root)
    except (OSError, RuntimeError, ValueError) as error:
        raise ValueError("Project metadata file escapes the project root") from error
    if not selected.is_file():
        raise ValueError("Project metadata file is not a regular file")
    return selected


def _write_member(archive: zipfile.ZipFile, name: str, payload: bytes) -> None:
    info = zipfile.ZipInfo(name, date_time=WHEEL_TIMESTAMP)
    info.compress_type = zipfile.ZIP_DEFLATED
    info.create_system = 3
    info.external_attr = 0o100644 << 16
    archive.writestr(info, payload)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--out-dir",
        type=Path,
        default=PROJECT_ROOT / "dist",
        help="wheel output directory (default: ./dist)",
    )
    args = parser.parse_args(argv)
    wheel = build_wheel(PROJECT_ROOT, args.out_dir)
    digest = hashlib.sha256(wheel.read_bytes()).hexdigest()
    print(f"Built {wheel}")
    print(f"SHA256 {digest}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
