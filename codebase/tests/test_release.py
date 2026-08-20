from __future__ import annotations

import base64
import csv
import hashlib
import io
import json
import os
import subprocess
import sys
import tarfile
import tempfile
import tomllib
import unittest
import zipfile
from pathlib import Path

import swoon


PROJECT_ROOT = Path(__file__).resolve().parents[1]
REPOSITORY_ROOT = PROJECT_ROOT.parent
BUILD_SCRIPT = PROJECT_ROOT / "scripts" / "build_wheel.py"
SMOKE_SCRIPT = PROJECT_ROOT / "scripts" / "smoke_wheel.py"
BUILD_SDIST_SCRIPT = PROJECT_ROOT / "scripts" / "build_sdist.py"
SMOKE_SDIST_SCRIPT = PROJECT_ROOT / "scripts" / "smoke_sdist.py"
BUILD_SBOM_SCRIPT = PROJECT_ROOT / "scripts" / "build_sbom.py"
RELEASE_SCRIPT = PROJECT_ROOT / "scripts" / "release.py"
VERSION = swoon.__version__
WHEEL_VERSION = VERSION.replace("-", "_")
WHEEL_NAME = f"swoon_code-{WHEEL_VERSION}-py3-none-any.whl"
DIST_INFO = f"swoon_code-{WHEEL_VERSION}.dist-info"
SDIST_ROOT = f"swoon_code-{WHEEL_VERSION}"
SDIST_NAME = f"{SDIST_ROOT}.tar.gz"
SBOM_NAME = f"swoon_code-{WHEEL_VERSION}.spdx.json"


class ConsumerReleaseTests(unittest.TestCase):
    def test_project_and_runtime_versions_match(self) -> None:
        configuration = tomllib.loads(
            (PROJECT_ROOT / "pyproject.toml").read_text(encoding="utf-8")
        )
        self.assertEqual(configuration["project"]["version"], swoon.__version__)
        self.assertEqual(configuration["project"]["license"], "Apache-2.0")
        self.assertEqual(
            configuration["project"]["license-files"],
            ["LICENSE", "NOTICE"],
        )
        self.assertEqual(configuration["project"]["requires-python"], ">=3.11,<3.15")
        self.assertEqual(
            configuration["project"]["authors"],
            [{"name": "Swoon Code contributors"}],
        )
        self.assertEqual(
            configuration["project"]["urls"]["Repository"],
            "https://github.com/dragon-tensor/swoon-code.git",
        )
        for minor in range(11, 15):
            self.assertIn(
                f"Programming Language :: Python :: 3.{minor}",
                configuration["project"]["classifiers"],
            )
        self.assertIn("setuptools>=77", configuration["build-system"]["requires"])

    def test_source_distribution_has_clear_legal_and_responsible_use_files(self) -> None:
        license_text = (PROJECT_ROOT / "LICENSE").read_text(encoding="utf-8")
        notice = (PROJECT_ROOT / "NOTICE").read_text(encoding="utf-8")
        responsible_use = (PROJECT_ROOT / "RESPONSIBLE_USE.md").read_text(
            encoding="utf-8"
        )

        self.assertIn("Apache License\n                           Version 2.0", license_text)
        self.assertIn("3. Grant of Patent License.", license_text)
        self.assertIn("7. Disclaimer of Warranty.", license_text)
        self.assertIn("END OF TERMS AND CONDITIONS", license_text)
        self.assertIn("Copyright 2026 Swoon Code contributors", notice)
        self.assertIn("not affiliated with, sponsored by, or endorsed by OpenAI", notice)
        self.assertIn("Educational purpose", responsible_use)
        self.assertIn("Each user is responsible", responsible_use)
        self.assertIn("does not add a restriction", responsible_use)

    def test_release_governance_documents_are_present_and_cross_linked(self) -> None:
        required = {
            "CHANGELOG.md": "Release candidate: 0.1.0",
            "CONTRIBUTING.md": "Security-sensitive changes",
            "SECURITY.md": "private vulnerability reporting form",
            "SUPPORT.md": "Do not attach cookie files",
            "THIRD_PARTY_NOTICES.md": "Playwright for Python",
            "docs/release-checklist.md": "Never place credentials in CI",
        }
        for relative, marker in required.items():
            text = (PROJECT_ROOT / relative).read_text(encoding="utf-8")
            self.assertIn(marker, text, relative)

        readme = (PROJECT_ROOT / "README.md").read_text(encoding="utf-8")
        self.assertIn("Reproducible wheel/source releases", readme)
        self.assertIn("docs/release-checklist.md", readme)

    def test_offline_builder_creates_a_complete_verifiable_wheel(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory)
            completed = self._run(BUILD_SCRIPT, "--out-dir", str(output))
            wheel = output / WHEEL_NAME

            self.assertEqual(completed.returncode, 0, completed.stderr)
            self.assertTrue(wheel.is_file())
            self.assertIn("SHA256", completed.stdout)
            second_output = output / "second"
            repeated = self._run(BUILD_SCRIPT, "--out-dir", str(second_output))
            self.assertEqual(repeated.returncode, 0, repeated.stderr)
            self.assertEqual(wheel.read_bytes(), (second_output / WHEEL_NAME).read_bytes())
            with zipfile.ZipFile(wheel) as archive:
                names = set(archive.namelist())
                self.assertIn("swoon/__init__.py", names)
                self.assertIn("swoon/__main__.py", names)
                self.assertIn(f"{DIST_INFO}/METADATA", names)
                self.assertIn(f"{DIST_INFO}/entry_points.txt", names)
                self.assertIn(f"{DIST_INFO}/RECORD", names)
                self.assertIn(f"{DIST_INFO}/licenses/LICENSE", names)
                self.assertIn(f"{DIST_INFO}/licenses/NOTICE", names)
                self.assertFalse(any("cookies" in name.casefold() for name in names))
                self.assertFalse(any(name.startswith("tests/") for name in names))
                metadata = archive.read(f"{DIST_INFO}/METADATA").decode("utf-8")
                self.assertIn("Metadata-Version: 2.4\n", metadata)
                self.assertIn("Name: swoon-code\n", metadata)
                self.assertIn(f"Version: {VERSION}\n", metadata)
                self.assertIn("Requires-Python: >=3.11,<3.15\n", metadata)
                self.assertIn("License-Expression: Apache-2.0\n", metadata)
                self.assertIn("Author: Swoon Code contributors\n", metadata)
                self.assertIn(
                    "Keywords: aeml,agent,coding-agent,education,interpreter\n",
                    metadata,
                )
                self.assertIn("Classifier: Development Status :: 3 - Alpha\n", metadata)
                self.assertIn(
                    "Project-URL: Repository, "
                    "https://github.com/dragon-tensor/swoon-code.git\n",
                    metadata,
                )
                self.assertIn("License-File: LICENSE\n", metadata)
                self.assertIn("License-File: NOTICE\n", metadata)
                self.assertIn("Requires-Dist: playwright>=1.50,<2\n", metadata)
                self.assertEqual(
                    archive.read(f"{DIST_INFO}/licenses/LICENSE"),
                    (PROJECT_ROOT / "LICENSE").read_bytes(),
                )
                self.assertEqual(
                    archive.read(f"{DIST_INFO}/licenses/NOTICE"),
                    (PROJECT_ROOT / "NOTICE").read_bytes(),
                )
                entry_points = archive.read(f"{DIST_INFO}/entry_points.txt").decode("utf-8")
                self.assertEqual(
                    entry_points,
                    "[console_scripts]\nswoon = swoon.cli:main\n",
                )
                self._verify_record(archive)

    def test_wheel_installs_and_runs_as_a_networkless_consumer(self) -> None:
        completed = self._run(SMOKE_SCRIPT, timeout=180)

        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertIn("Consumer wheel smoke test passed.", completed.stdout)

    def test_offline_builder_creates_a_reproducible_safe_source_archive(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory)
            completed = self._run(BUILD_SDIST_SCRIPT, "--out-dir", str(output))
            source_archive = output / SDIST_NAME

            self.assertEqual(completed.returncode, 0, completed.stderr)
            self.assertTrue(source_archive.is_file())
            self.assertIn("SHA256", completed.stdout)
            repeated_output = output / "second"
            repeated = self._run(
                BUILD_SDIST_SCRIPT,
                "--out-dir",
                str(repeated_output),
            )
            self.assertEqual(repeated.returncode, 0, repeated.stderr)
            self.assertEqual(
                source_archive.read_bytes(),
                (repeated_output / SDIST_NAME).read_bytes(),
            )

            with tarfile.open(source_archive, mode="r:gz") as archive:
                members = archive.getmembers()
                names = {member.name for member in members}
                required = {
                    SDIST_ROOT,
                    f"{SDIST_ROOT}/PKG-INFO",
                    f"{SDIST_ROOT}/pyproject.toml",
                    f"{SDIST_ROOT}/README.md",
                    f"{SDIST_ROOT}/LICENSE",
                    f"{SDIST_ROOT}/NOTICE",
                    f"{SDIST_ROOT}/THIRD_PARTY_NOTICES.md",
                    f"{SDIST_ROOT}/.github/workflows/ci.yml",
                    f"{SDIST_ROOT}/scripts/build_sdist.py",
                    f"{SDIST_ROOT}/tests/fixtures/aeml_security_corpus.json",
                    f"{SDIST_ROOT}/swoon/__init__.py",
                }
                self.assertFalse(required - names)
                self.assertFalse(any(member.issym() or member.islnk() for member in members))
                self.assertTrue(all(member.uid == 0 and member.gid == 0 for member in members))
                self.assertTrue(all(member.mtime == 0 for member in members))
                self.assertFalse(any("__pycache__" in name for name in names))
                self.assertFalse(any("cookies.json" in name for name in names))
                self.assertIn(f"{SDIST_ROOT}/cookies.example.json", names)
                by_name = {member.name: member for member in members}
                self.assertEqual(
                    by_name[f"{SDIST_ROOT}/scripts/release.py"].mode,
                    0o755,
                )
                self.assertEqual(by_name[f"{SDIST_ROOT}/README.md"].mode, 0o644)
                metadata_file = archive.extractfile(f"{SDIST_ROOT}/PKG-INFO")
                self.assertIsNotNone(metadata_file)
                assert metadata_file is not None
                metadata = metadata_file.read().decode("utf-8")
                self.assertIn("Metadata-Version: 2.4\n", metadata)
                self.assertIn(f"Version: {VERSION}\n", metadata)
                self.assertIn("License-File: LICENSE\n", metadata)
                self.assertIn("License-File: NOTICE\n", metadata)

    def test_source_archive_rebuilds_and_runs_as_a_networkless_consumer(self) -> None:
        completed = self._run(SMOKE_SDIST_SCRIPT, timeout=240)

        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertIn(
            "Consumer source-distribution smoke test passed.",
            completed.stdout,
        )

    def test_sbom_is_reproducible_and_describes_direct_dependencies(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory)
            completed = self._run(
                BUILD_SBOM_SCRIPT,
                "--out-dir",
                str(output),
                "--source-date-epoch",
                "0",
            )
            sbom = output / SBOM_NAME
            self.assertEqual(completed.returncode, 0, completed.stderr)
            self.assertTrue(sbom.is_file())
            repeated_output = output / "second"
            repeated = self._run(
                BUILD_SBOM_SCRIPT,
                "--out-dir",
                str(repeated_output),
                "--source-date-epoch",
                "0",
            )
            self.assertEqual(repeated.returncode, 0, repeated.stderr)
            self.assertEqual(sbom.read_bytes(), (repeated_output / SBOM_NAME).read_bytes())

            document = json.loads(sbom.read_text(encoding="utf-8"))
            self.assertEqual(document["spdxVersion"], "SPDX-2.3")
            self.assertEqual(document["dataLicense"], "CC0-1.0")
            self.assertEqual(document["creationInfo"]["created"], "1970-01-01T00:00:00Z")
            packages = {package["name"]: package for package in document["packages"]}
            self.assertEqual(set(packages), {"swoon-code", "playwright"})
            self.assertEqual(packages["swoon-code"]["versionInfo"], VERSION)
            self.assertEqual(packages["swoon-code"]["licenseDeclared"], "Apache-2.0")
            self.assertEqual(packages["playwright"]["licenseDeclared"], "Apache-2.0")
            self.assertIn(
                {
                    "spdxElementId": "SPDXRef-Package-swoon-code",
                    "relationshipType": "DEPENDS_ON",
                    "relatedSpdxElement": "SPDXRef-Package-playwright",
                },
                document["relationships"],
            )

    def test_release_driver_rejects_nonempty_output_and_wrong_tags(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory)
            (output / "keep.txt").write_text("do not overwrite\n", encoding="utf-8")
            completed = self._run(
                RELEASE_SCRIPT,
                "--allow-dirty",
                "--out-dir",
                str(output),
            )
            self.assertEqual(completed.returncode, 1)
            self.assertIn("must be empty", completed.stderr)
            self.assertEqual(
                (output / "keep.txt").read_text(encoding="utf-8"),
                "do not overwrite\n",
            )

        with tempfile.TemporaryDirectory() as directory:
            completed = self._run(
                RELEASE_SCRIPT,
                "--allow-dirty",
                "--expected-tag",
                "v999.0.0",
                "--out-dir",
                directory,
            )
            self.assertEqual(completed.returncode, 1)
            self.assertIn(f"exactly v{VERSION}", completed.stderr)

    def test_workflows_keep_credentials_out_of_automation(self) -> None:
        continuous = (REPOSITORY_ROOT / ".github/workflows/ci.yml").read_text(
            encoding="utf-8"
        )
        release = (REPOSITORY_ROOT / ".github/workflows/release.yml").read_text(
            encoding="utf-8"
        )
        combined = continuous + release
        self.assertIn('python: ["3.11", "3.12", "3.13", "3.14"]', continuous)
        self.assertIn("ubuntu-24.04-arm", continuous)
        self.assertIn("swoon doctor --launch-browser", continuous)
        self.assertIn("actions/attest@v4", release)
        self.assertIn("--draft", release)
        self.assertNotIn("live_acceptance.py", combined)
        self.assertNotIn("--cookies", combined)

    def _verify_record(self, archive: zipfile.ZipFile) -> None:
        record_name = f"{DIST_INFO}/RECORD"
        rows = list(
            csv.reader(
                io.StringIO(archive.read(record_name).decode("utf-8"), newline="")
            )
        )
        recorded_names = {row[0] for row in rows}
        self_names = set(archive.namelist())
        self.assertEqual(recorded_names, self_names)
        for name, digest_field, size_field in rows:
            if name == record_name:
                self.assertEqual((digest_field, size_field), ("", ""))
                continue
            payload = archive.read(name)
            algorithm, encoded = digest_field.split("=", 1)
            self.assertEqual(algorithm, "sha256")
            padding = "=" * (-len(encoded) % 4)
            self.assertEqual(
                base64.urlsafe_b64decode(encoded + padding),
                hashlib.sha256(payload).digest(),
            )
            self.assertEqual(int(size_field), len(payload))

    @staticmethod
    def _run(
        script: Path,
        *arguments: str,
        timeout: int = 60,
    ) -> subprocess.CompletedProcess[str]:
        environment = dict(os.environ)
        environment.update(
            {
                "PIP_DISABLE_PIP_VERSION_CHECK": "1",
                "PIP_NO_CACHE_DIR": "1",
                "PIP_NO_INDEX": "1",
                "PYTHONDONTWRITEBYTECODE": "1",
            }
        )
        return subprocess.run(
            [sys.executable, str(script), *arguments],
            cwd=PROJECT_ROOT,
            check=False,
            capture_output=True,
            text=True,
            timeout=timeout,
            env=environment,
        )


if __name__ == "__main__":
    unittest.main()
