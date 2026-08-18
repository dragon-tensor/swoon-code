#!/usr/bin/env python3
"""Build a deterministic SPDX 2.3 SBOM for Swoon Code's direct dependencies."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from build_wheel import (
    PROJECT_ROOT,
    _normalize_distribution,
    _normalize_version,
    _project_configuration,
    _required_text,
    project_version,
)


KNOWN_DEPENDENCIES = {
    "playwright": {
        "license": "Apache-2.0",
        "download": "https://pypi.org/project/playwright/",
        "homepage": "https://github.com/microsoft/playwright-python",
    },
}


def build_sbom(
    project_root: Path,
    output_directory: Path,
    *,
    source_date_epoch: int = 0,
) -> Path:
    """Create and return one deterministic direct-dependency SPDX document."""

    root = project_root.resolve(strict=True)
    if source_date_epoch < 0:
        raise ValueError("source-date epoch cannot be negative")
    try:
        created = datetime.fromtimestamp(source_date_epoch, timezone.utc).strftime(
            "%Y-%m-%dT%H:%M:%SZ"
        )
    except (OverflowError, OSError, ValueError) as error:
        raise ValueError("source-date epoch is outside the supported date range") from error

    project = _project_configuration(root)["project"]
    name = _required_text(project, "name")
    version = project_version(root)
    if _required_text(project, "license") != "Apache-2.0":
        raise ValueError("SBOM metadata must be reviewed when the project license changes")
    dependencies = _dependencies(project)
    identity = _source_identity(root, source_date_epoch)
    document_namespace = (
        "https://github.com/dragon-tensor/swoon-code/spdx/"
        f"{version}/{identity}"
    )
    project_id = "SPDXRef-Package-swoon-code"

    packages: list[dict[str, Any]] = [
        {
            "SPDXID": project_id,
            "name": name,
            "versionInfo": version,
            "downloadLocation": "https://github.com/dragon-tensor/swoon-code",
            "filesAnalyzed": False,
            "homepage": "https://github.com/dragon-tensor/swoon-code",
            "licenseConcluded": "Apache-2.0",
            "licenseDeclared": "Apache-2.0",
            "copyrightText": "Copyright 2026 Swoon Code contributors",
            "primaryPackagePurpose": "APPLICATION",
            "supplier": "Organization: Swoon Code contributors",
            "externalRefs": [
                {
                    "referenceCategory": "PACKAGE-MANAGER",
                    "referenceType": "purl",
                    "referenceLocator": f"pkg:pypi/{name}@{version}",
                }
            ],
        }
    ]
    relationships: list[dict[str, str]] = [
        {
            "spdxElementId": "SPDXRef-DOCUMENT",
            "relationshipType": "DESCRIBES",
            "relatedSpdxElement": project_id,
        }
    ]
    for dependency_name, requirement in dependencies:
        details = KNOWN_DEPENDENCIES.get(dependency_name)
        if details is None:
            raise ValueError(
                f"No reviewed SBOM metadata exists for direct dependency {dependency_name!r}"
            )
        dependency_id = f"SPDXRef-Package-{dependency_name.replace('-', '_')}"
        packages.append(
            {
                "SPDXID": dependency_id,
                "name": dependency_name,
                "downloadLocation": details["download"],
                "filesAnalyzed": False,
                "homepage": details["homepage"],
                "licenseConcluded": details["license"],
                "licenseDeclared": details["license"],
                "copyrightText": "NOASSERTION",
                "primaryPackagePurpose": "LIBRARY",
                "supplier": "NOASSERTION",
                "comment": f"Direct dependency declared as {requirement}",
                "externalRefs": [
                    {
                        "referenceCategory": "PACKAGE-MANAGER",
                        "referenceType": "purl",
                        "referenceLocator": f"pkg:pypi/{dependency_name}",
                    }
                ],
            }
        )
        relationships.append(
            {
                "spdxElementId": project_id,
                "relationshipType": "DEPENDS_ON",
                "relatedSpdxElement": dependency_id,
            }
        )

    document = {
        "SPDXID": "SPDXRef-DOCUMENT",
        "spdxVersion": "SPDX-2.3",
        "dataLicense": "CC0-1.0",
        "name": f"{name}-{version}",
        "documentNamespace": document_namespace,
        "creationInfo": {
            "created": created,
            "creators": ["Tool: swoon-direct-dependency-sbom-builder-1"],
        },
        "documentDescribes": [project_id],
        "packages": packages,
        "relationships": relationships,
        "comment": (
            "Direct-dependency SBOM generated from pyproject.toml; browser binaries and "
            "system prerequisites are installed separately and are not resolved components."
        ),
    }
    payload = (json.dumps(document, indent=2, sort_keys=True) + "\n").encode("utf-8")

    output = output_directory.resolve()
    output.mkdir(mode=0o755, parents=True, exist_ok=True)
    if not output.is_dir():
        raise ValueError("SBOM output path is not a directory")
    filename = (
        f"{_normalize_distribution(name)}-{_normalize_version(version)}.spdx.json"
    )
    return _atomic_write(output / filename, payload)


def _dependencies(project: dict[str, Any]) -> tuple[tuple[str, str], ...]:
    raw = project.get("dependencies")
    if not isinstance(raw, list) or not raw:
        raise ValueError("project dependencies must be a non-empty array")
    selected: list[tuple[str, str]] = []
    names: set[str] = set()
    for requirement in raw:
        if not isinstance(requirement, str) or "\n" in requirement or "\r" in requirement:
            raise ValueError("project dependency requirements must be one-line text")
        match = re.fullmatch(r"\s*([A-Za-z0-9][A-Za-z0-9._-]*)(.*)\s*", requirement)
        if match is None:
            raise ValueError(f"Cannot parse direct dependency requirement: {requirement!r}")
        name = re.sub(r"[-_.]+", "-", match.group(1)).lower()
        normalized_requirement = requirement.strip()
        if name in names:
            raise ValueError(f"Duplicate direct dependency: {name}")
        names.add(name)
        selected.append((name, normalized_requirement))
    return tuple(sorted(selected))


def _source_identity(root: Path, source_date_epoch: int) -> str:
    digest = hashlib.sha256()
    digest.update(f"epoch:{source_date_epoch}\n".encode("ascii"))
    candidates = [root / "pyproject.toml", *(root / "swoon").rglob("*.py")]
    for source in sorted(candidates):
        relative = source.relative_to(root).as_posix()
        if source.is_symlink() or not source.is_file():
            raise ValueError(f"SBOM identity input is not a regular file: {relative}")
        digest.update(relative.encode("utf-8") + b"\0")
        digest.update(source.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()


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


def _source_date_epoch(value: str | None) -> int:
    if value is None:
        return 0
    try:
        selected = int(value)
    except ValueError as error:
        raise ValueError("SOURCE_DATE_EPOCH must be a whole number") from error
    if selected < 0:
        raise ValueError("SOURCE_DATE_EPOCH cannot be negative")
    return selected


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--out-dir",
        type=Path,
        default=PROJECT_ROOT / "dist",
        help="SBOM output directory (default: ./dist)",
    )
    parser.add_argument(
        "--source-date-epoch",
        type=int,
        default=None,
        help="creation timestamp; defaults to SOURCE_DATE_EPOCH or the Unix epoch",
    )
    args = parser.parse_args(argv)
    try:
        epoch = (
            args.source_date_epoch
            if args.source_date_epoch is not None
            else _source_date_epoch(os.environ.get("SOURCE_DATE_EPOCH"))
        )
        artifact = build_sbom(PROJECT_ROOT, args.out_dir, source_date_epoch=epoch)
    except (OSError, ValueError) as error:
        parser.error(str(error))
    digest = hashlib.sha256(artifact.read_bytes()).hexdigest()
    print(f"Built {artifact}")
    print(f"SHA256 {digest}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
