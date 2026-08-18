#!/usr/bin/env python3
"""Safely unpack, rebuild, and consumer-test a Swoon source distribution."""

from __future__ import annotations

import argparse
import os
import stat
import subprocess
import sys
import tarfile
import tempfile
from pathlib import Path, PurePosixPath

from build_sdist import build_sdist
from build_wheel import PROJECT_ROOT, _normalize_distribution, project_version


MAX_MEMBERS = 2_500
MAX_MEMBER_BYTES = 8 * 1024 * 1024
MAX_ARCHIVE_BYTES = 40 * 1024 * 1024
MAX_COMPRESSED_BYTES = 16 * 1024 * 1024


def smoke_sdist(source_archive: Path | None = None) -> None:
    version = project_version(PROJECT_ROOT)
    expected_root = f"{_normalize_distribution('swoon-code')}-{version.replace('-', '_')}"
    with tempfile.TemporaryDirectory(prefix="swoon-sdist-smoke-") as directory:
        temporary = Path(directory)
        selected = (
            _select_archive(source_archive)
            if source_archive is not None
            else build_sdist(PROJECT_ROOT, temporary / "source")
        )
        extraction = temporary / "extracted"
        extraction.mkdir(mode=0o700)

        with tarfile.open(selected, mode="r:gz") as archive:
            members: list[tarfile.TarInfo] = []
            for member in archive:
                if len(members) >= MAX_MEMBERS:
                    raise RuntimeError("Source distribution has too many members")
                members.append(member)
            _validate_members(members, expected_root)
            package_info = archive.extractfile(f"{expected_root}/PKG-INFO")
            if package_info is None:
                raise RuntimeError("Source distribution has no readable PKG-INFO")
            metadata = package_info.read().decode("utf-8")
            if f"Version: {version}\n" not in metadata:
                raise RuntimeError("Source-distribution metadata version is incorrect")
            archive.extractall(extraction, members=members, filter="data")

        source_root = extraction / expected_root
        if not (source_root / "pyproject.toml").is_file():
            raise RuntimeError("Extracted source distribution has no pyproject.toml")
        wheel_directory = temporary / "wheel"
        _run(
            source_root / "scripts" / "build_wheel.py",
            "--out-dir",
            str(wheel_directory),
            cwd=source_root,
        )
        wheels = tuple(wheel_directory.glob("*.whl"))
        if len(wheels) != 1:
            raise RuntimeError("Extracted source distribution did not build exactly one wheel")
        _run(
            source_root / "scripts" / "smoke_wheel.py",
            str(wheels[0]),
            cwd=source_root,
            timeout=180,
        )


def _select_archive(candidate: Path) -> Path:
    selected = candidate.expanduser()
    if selected.is_symlink():
        raise RuntimeError("Source-distribution input cannot be a symbolic link")
    try:
        details = selected.stat(follow_symlinks=False)
    except OSError as error:
        raise RuntimeError("Source-distribution input is not readable") from error
    if not stat.S_ISREG(details.st_mode):
        raise RuntimeError("Source-distribution input must be a regular file")
    if details.st_size > MAX_COMPRESSED_BYTES:
        raise RuntimeError("Compressed source distribution exceeds 16 MiB")
    return selected.resolve(strict=True)


def _validate_members(members: list[tarfile.TarInfo], expected_root: str) -> None:
    if not members or len(members) > MAX_MEMBERS:
        raise RuntimeError("Source distribution has an invalid member count")
    names: set[str] = set()
    total_size = 0
    for member in members:
        name = member.name
        path = PurePosixPath(name)
        if (
            not name
            or "\\" in name
            or path.is_absolute()
            or any(part in {"", ".", ".."} for part in path.parts)
            or path.parts[0] != expected_root
        ):
            raise RuntimeError(f"Unsafe source-distribution member: {name!r}")
        if name in names:
            raise RuntimeError(f"Duplicate source-distribution member: {name!r}")
        if not (member.isdir() or member.isfile()):
            raise RuntimeError(f"Unsupported source-distribution member: {name!r}")
        if member.size < 0 or member.size > MAX_MEMBER_BYTES:
            raise RuntimeError(f"Oversized source-distribution member: {name!r}")
        total_size += member.size
        if total_size > MAX_ARCHIVE_BYTES:
            raise RuntimeError("Expanded source distribution exceeds 40 MiB")
        names.add(name)

    required = {
        expected_root,
        f"{expected_root}/PKG-INFO",
        f"{expected_root}/pyproject.toml",
        f"{expected_root}/scripts/build_wheel.py",
        f"{expected_root}/scripts/smoke_wheel.py",
        f"{expected_root}/swoon/__init__.py",
        f"{expected_root}/LICENSE",
        f"{expected_root}/NOTICE",
    }
    missing = required - names
    if missing:
        raise RuntimeError(
            "Source distribution is incomplete: " + ", ".join(sorted(missing))
        )


def _run(
    script: Path,
    *arguments: str,
    cwd: Path,
    timeout: int = 120,
) -> None:
    environment = dict(os.environ)
    environment.update(
        {
            "PIP_DISABLE_PIP_VERSION_CHECK": "1",
            "PIP_NO_CACHE_DIR": "1",
            "PIP_NO_INDEX": "1",
            "PYTHONDONTWRITEBYTECODE": "1",
        }
    )
    completed = subprocess.run(
        [sys.executable, str(script), *arguments],
        cwd=cwd,
        check=False,
        capture_output=True,
        text=True,
        timeout=timeout,
        env=environment,
    )
    if completed.returncode != 0:
        raise RuntimeError(
            f"Source consumer command failed ({completed.returncode}): "
            f"{completed.stderr.strip() or completed.stdout.strip()}"
        )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "source_archive",
        nargs="?",
        type=Path,
        help="existing .tar.gz; builds one if omitted",
    )
    args = parser.parse_args(argv)
    smoke_sdist(args.source_archive)
    print("Consumer source-distribution smoke test passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
