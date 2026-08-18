#!/usr/bin/env python3
"""Run offline release gates and assemble Swoon Code release artifacts."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path

from build_sbom import build_sbom
from build_sdist import build_sdist
from build_wheel import (
    PROJECT_ROOT,
    build_wheel,
    project_version,
)


MEDIA_TYPES = {
    ".whl": "application/vnd.pypi.wheel+zip",
    ".gz": "application/vnd.pypi.sdist+gzip",
    ".json": "application/spdx+json",
}


def assemble_release(
    output_directory: Path,
    *,
    allow_dirty: bool = False,
    expected_tag: str | None = None,
) -> tuple[Path, ...]:
    """Run all release checks and return the assembled artifact paths."""

    version = project_version(PROJECT_ROOT)
    state = _repository_state()
    dirty = bool(state["status"])
    if dirty and not allow_dirty:
        raise RuntimeError(
            "Release requires a clean Git worktree; commit/stash changes or use "
            "--allow-dirty for a non-publishable rehearsal"
        )
    if expected_tag is not None:
        _verify_tag(expected_tag, version, state["commit"])

    output = output_directory.resolve()
    git_metadata = PROJECT_ROOT / ".git"
    if (
        output == PROJECT_ROOT
        or output == git_metadata
        or git_metadata in output.parents
    ):
        raise RuntimeError("Release output cannot be the project or Git metadata directory")
    if output.exists():
        if not output.is_dir():
            raise RuntimeError("Release output exists and is not a directory")
        if any(output.iterdir()):
            raise RuntimeError("Release output directory must be empty")
    else:
        output.mkdir(mode=0o755, parents=True)

    environment = _release_environment()
    _run(
        [sys.executable, "-m", "unittest", "discover", "-s", "tests", "-v"],
        environment=environment,
        label="unit test suite",
        timeout=600,
    )
    _run(
        [sys.executable, "scripts/aeml_eval.py"],
        environment=environment,
        label="adversarial AEML corpus",
    )
    _assert_repository_unchanged(state)

    wheel = build_wheel(PROJECT_ROOT, output)
    source_archive = build_sdist(PROJECT_ROOT, output)
    sbom = build_sbom(
        PROJECT_ROOT,
        output,
        source_date_epoch=int(state["timestamp"]),
    )
    _run(
        [sys.executable, "scripts/smoke_wheel.py", str(wheel)],
        environment=environment,
        label="installed wheel smoke",
        timeout=300,
    )
    _run(
        [sys.executable, "scripts/smoke_sdist.py", str(source_archive)],
        environment=environment,
        label="source-distribution smoke",
        timeout=300,
    )
    _verify_reproducible(
        wheel,
        source_archive,
        sbom,
        source_date_epoch=int(state["timestamp"]),
    )
    _assert_repository_unchanged(state)

    primary = (wheel, source_archive, sbom)
    manifest = _write_manifest(
        output,
        primary,
        version=version,
        commit=str(state["commit"]),
        source_date_epoch=int(state["timestamp"]),
        dirty=dirty,
    )
    checksums = _write_checksums(output, (*primary, manifest))
    return (*primary, manifest, checksums)


def _repository_state() -> dict[str, str]:
    commit = _git("rev-parse", "HEAD")
    timestamp = _git("show", "-s", "--format=%ct", "HEAD")
    if not re.fullmatch(r"[0-9a-f]{40}", commit):
        raise RuntimeError("Git HEAD is not a full commit identifier")
    if not timestamp.isdecimal():
        raise RuntimeError("Git HEAD has an invalid commit timestamp")
    status = _git("status", "--porcelain=v1", "--untracked-files=all")
    return {"commit": commit, "timestamp": timestamp, "status": status}


def _verify_tag(expected_tag: str, version: str, commit: str) -> None:
    if expected_tag != f"v{version}" or not re.fullmatch(r"v[0-9A-Za-z._+-]+", expected_tag):
        raise RuntimeError(f"Release tag must be exactly v{version}")
    tagged_commit = _git(
        "rev-parse",
        "--verify",
        f"refs/tags/{expected_tag}^{{commit}}",
    )
    if tagged_commit != commit:
        raise RuntimeError(f"Tag {expected_tag} does not resolve to the checked-out commit")


def _assert_repository_unchanged(expected: dict[str, str]) -> None:
    current = _repository_state()
    if current != expected:
        raise RuntimeError("Git repository changed while release gates were running")


def _verify_reproducible(
    wheel: Path,
    source_archive: Path,
    sbom: Path,
    *,
    source_date_epoch: int,
) -> None:
    with tempfile.TemporaryDirectory(prefix="swoon-reproducibility-") as directory:
        repeated = Path(directory)
        comparisons = (
            (wheel, build_wheel(PROJECT_ROOT, repeated)),
            (source_archive, build_sdist(PROJECT_ROOT, repeated)),
            (
                sbom,
                build_sbom(
                    PROJECT_ROOT,
                    repeated,
                    source_date_epoch=source_date_epoch,
                ),
            ),
        )
        for original, rebuilt in comparisons:
            if original.read_bytes() != rebuilt.read_bytes():
                raise RuntimeError(f"Release artifact is not reproducible: {original.name}")


def _write_manifest(
    output: Path,
    artifacts: tuple[Path, ...],
    *,
    version: str,
    commit: str,
    source_date_epoch: int,
    dirty: bool,
) -> Path:
    records = []
    for artifact in sorted(artifacts, key=lambda path: path.name):
        records.append(
            {
                "filename": artifact.name,
                "mediaType": _media_type(artifact),
                "sha256": _sha256(artifact),
                "size": artifact.stat().st_size,
            }
        )
    manifest = {
        "schemaVersion": 1,
        "project": "swoon-code",
        "version": version,
        "commit": commit,
        "sourceDateEpoch": source_date_epoch,
        "dirty": dirty,
        "artifacts": records,
    }
    payload = (json.dumps(manifest, indent=2, sort_keys=True) + "\n").encode("utf-8")
    return _atomic_write(output / "release-manifest.json", payload)


def _write_checksums(output: Path, artifacts: tuple[Path, ...]) -> Path:
    lines = [f"{_sha256(path)}  {path.name}" for path in sorted(artifacts)]
    return _atomic_write(output / "SHA256SUMS", ("\n".join(lines) + "\n").encode("ascii"))


def _atomic_write(target: Path, payload: bytes) -> Path:
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{target.name}.",
        suffix=".tmp",
        dir=target.parent,
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        temporary.chmod(0o644)
        os.replace(temporary, target)
    finally:
        temporary.unlink(missing_ok=True)
    return target


def _media_type(path: Path) -> str:
    for suffix, media_type in MEDIA_TYPES.items():
        if path.name.endswith(suffix):
            return media_type
    raise RuntimeError(f"No release media type is registered for {path.name}")


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while block := stream.read(1024 * 1024):
            digest.update(block)
    return digest.hexdigest()


def _release_environment() -> dict[str, str]:
    environment = dict(os.environ)
    environment.update(
        {
            "PIP_DISABLE_PIP_VERSION_CHECK": "1",
            "PIP_NO_CACHE_DIR": "1",
            "PIP_NO_INDEX": "1",
            "PYTHONDONTWRITEBYTECODE": "1",
        }
    )
    return environment


def _run(
    command: list[str],
    *,
    environment: dict[str, str],
    label: str,
    timeout: int = 180,
) -> None:
    print(f"Running {label}...", flush=True)
    completed = subprocess.run(
        command,
        cwd=PROJECT_ROOT,
        check=False,
        timeout=timeout,
        env=environment,
    )
    if completed.returncode != 0:
        raise RuntimeError(f"{label} failed with exit code {completed.returncode}")


def _git(*arguments: str) -> str:
    completed = subprocess.run(
        ["git", *arguments],
        cwd=PROJECT_ROOT,
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )
    if completed.returncode != 0:
        detail = completed.stderr.strip() or completed.stdout.strip()
        raise RuntimeError(f"Git command failed: {detail}")
    return completed.stdout.strip()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--out-dir",
        type=Path,
        default=PROJECT_ROOT / "dist",
        help="empty release output directory (default: ./dist)",
    )
    parser.add_argument(
        "--allow-dirty",
        action="store_true",
        help="allow a dirty-tree rehearsal; resulting manifest is marked dirty",
    )
    parser.add_argument(
        "--expected-tag",
        help="require this vVERSION tag to resolve to HEAD",
    )
    args = parser.parse_args(argv)
    try:
        artifacts = assemble_release(
            args.out_dir,
            allow_dirty=args.allow_dirty,
            expected_tag=args.expected_tag,
        )
    except (OSError, RuntimeError, ValueError) as error:
        print(f"Release failed: {error}", file=sys.stderr)
        return 1
    print("Release gates passed.")
    for artifact in artifacts:
        print(f"{_sha256(artifact)}  {artifact}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
