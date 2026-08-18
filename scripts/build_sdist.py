#!/usr/bin/env python3
"""Build Swoon Code's deterministic source distribution without build tooling."""

from __future__ import annotations

import argparse
import gzip
import hashlib
import os
import stat
import tarfile
import tempfile
from pathlib import Path

from build_wheel import (
    PROJECT_ROOT,
    _license_files,
    _metadata,
    _normalize_distribution,
    _normalize_version,
    _project_configuration,
    _required_text,
    project_version,
)


ROOT_FILES = (
    ".gitignore",
    "CHANGELOG.md",
    "CONTRIBUTING.md",
    "LICENSE",
    "MIGRATION.md",
    "NOTICE",
    "README.md",
    "RESPONSIBLE_USE.md",
    "SECURITY.md",
    "SUPPORT.md",
    "THIRD_PARTY_NOTICES.md",
    "aeml_protocol_spec.md",
    "chatgpt.sh",
    "chatgpt_agent.py",
    "cookies.example.json",
    "pyproject.toml",
)
SOURCE_DIRECTORIES = {
    ".github": frozenset({".yml", ".yaml"}),
    "docs": frozenset({".md"}),
    "scripts": frozenset({".py"}),
    "swoon": frozenset({".py", ".typed"}),
    "tests": frozenset({".py", ".json"}),
}
MAX_SOURCE_FILES = 2_000
MAX_SOURCE_FILE_BYTES = 8 * 1024 * 1024
MAX_SOURCE_BYTES = 32 * 1024 * 1024


def build_sdist(project_root: Path, output_directory: Path) -> Path:
    """Build and return one reproducible PAX ``.tar.gz`` source archive."""

    root = project_root.resolve(strict=True)
    project = _project_configuration(root)["project"]
    name = _required_text(project, "name")
    version = project_version(root)
    distribution = _normalize_distribution(name)
    archive_version = _normalize_version(version)
    archive_root = f"{distribution}-{archive_version}"
    filename = f"{archive_root}.tar.gz"

    output = output_directory.resolve()
    output.mkdir(mode=0o755, parents=True, exist_ok=True)
    if not output.is_dir():
        raise ValueError("Source-distribution output path is not a directory")

    payloads: dict[str, tuple[bytes, int]] = {}
    total_bytes = 0
    for source in _source_files(root):
        relative = source.relative_to(root).as_posix()
        payload = source.read_bytes()
        if len(payload) > MAX_SOURCE_FILE_BYTES:
            raise ValueError(f"Source file exceeds 8 MiB: {relative}")
        total_bytes += len(payload)
        if total_bytes > MAX_SOURCE_BYTES:
            raise ValueError("Source distribution exceeds the 32 MiB input limit")
        mode = _source_mode(relative)
        payloads[f"{archive_root}/{relative}"] = (payload, mode)

    license_files = _license_files(project, root)
    metadata = _metadata(
        project,
        root,
        tuple(relative for relative, _ in license_files),
    )
    payloads[f"{archive_root}/PKG-INFO"] = (metadata, 0o644)

    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{filename}.",
        suffix=".tmp",
        dir=output,
    )
    temporary = Path(temporary_name)
    target = output / filename
    try:
        with os.fdopen(descriptor, "wb") as raw_archive:
            with gzip.GzipFile(
                filename="",
                mode="wb",
                fileobj=raw_archive,
                compresslevel=9,
                mtime=0,
            ) as compressed:
                with tarfile.open(
                    fileobj=compressed,
                    mode="w",
                    format=tarfile.PAX_FORMAT,
                ) as archive:
                    for directory in _archive_directories(archive_root, payloads):
                        info = _tar_info(directory, mode=0o755)
                        info.type = tarfile.DIRTYPE
                        archive.addfile(info)
                    for archive_name in sorted(payloads):
                        payload, mode = payloads[archive_name]
                        info = _tar_info(archive_name, mode=mode)
                        info.size = len(payload)
                        archive.addfile(info, fileobj=_BytesReader(payload))
        temporary.chmod(0o644)
        os.replace(temporary, target)
    finally:
        temporary.unlink(missing_ok=True)
    return target


def _source_files(root: Path) -> tuple[Path, ...]:
    selected: list[Path] = []
    for relative in ROOT_FILES:
        source = root / relative
        _validate_source_file(source, root)
        selected.append(source)

    for directory_name, suffixes in SOURCE_DIRECTORIES.items():
        directory = root / directory_name
        if directory.is_symlink() or not directory.is_dir():
            raise ValueError(f"Required source directory is invalid: {directory_name}")
        for source in sorted(directory.rglob("*")):
            relative = source.relative_to(root)
            if source.is_symlink():
                raise ValueError(f"Source distribution cannot contain a symlink: {relative}")
            details = source.stat(follow_symlinks=False)
            if stat.S_ISDIR(details.st_mode):
                continue
            if "__pycache__" in source.parts or source.suffix == ".pyc":
                continue
            if not stat.S_ISREG(details.st_mode):
                raise ValueError(f"Source distribution cannot contain a special file: {relative}")
            if source.suffix not in suffixes:
                raise ValueError(f"Unrecognized source file: {relative}")
            selected.append(source)

    if len(selected) > MAX_SOURCE_FILES:
        raise ValueError(f"Source distribution exceeds {MAX_SOURCE_FILES} files")
    relative_names = [source.relative_to(root).as_posix() for source in selected]
    if len(relative_names) != len(set(relative_names)):
        raise ValueError("Source distribution contains duplicate file paths")
    return tuple(sorted(selected))


def _validate_source_file(source: Path, root: Path) -> None:
    relative = source.relative_to(root)
    if source.is_symlink():
        raise ValueError(f"Required source file is a symlink: {relative}")
    try:
        details = source.stat(follow_symlinks=False)
    except OSError as error:
        raise ValueError(f"Required source file is missing: {relative}") from error
    if not stat.S_ISREG(details.st_mode):
        raise ValueError(f"Required source file is not regular: {relative}")


def _source_mode(relative: str) -> int:
    """Normalize permissions independently of checkout/platform mode behavior."""

    if relative == "chatgpt.sh" or (
        relative.startswith("scripts/") and relative.endswith(".py")
    ):
        return 0o755
    return 0o644


def _archive_directories(
    archive_root: str,
    payloads: dict[str, tuple[bytes, int]],
) -> tuple[str, ...]:
    directories = {archive_root}
    for archive_name in payloads:
        parent = Path(archive_name).parent
        while parent.as_posix() != ".":
            directories.add(parent.as_posix())
            parent = parent.parent
    return tuple(sorted(directories, key=lambda name: (name.count("/"), name)))


def _tar_info(name: str, *, mode: int) -> tarfile.TarInfo:
    info = tarfile.TarInfo(name)
    info.mode = mode
    info.mtime = 0
    info.uid = 0
    info.gid = 0
    info.uname = ""
    info.gname = ""
    return info


class _BytesReader:
    """Small file-like reader that avoids mutable in-memory stream state."""

    def __init__(self, payload: bytes) -> None:
        self._payload = payload
        self._offset = 0

    def read(self, size: int = -1) -> bytes:
        if size < 0:
            size = len(self._payload) - self._offset
        start = self._offset
        self._offset = min(len(self._payload), start + size)
        return self._payload[start : self._offset]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--out-dir",
        type=Path,
        default=PROJECT_ROOT / "dist",
        help="source-distribution output directory (default: ./dist)",
    )
    args = parser.parse_args(argv)
    archive = build_sdist(PROJECT_ROOT, args.out_dir)
    digest = hashlib.sha256(archive.read_bytes()).hexdigest()
    print(f"Built {archive}")
    print(f"SHA256 {digest}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
