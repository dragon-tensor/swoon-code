from __future__ import annotations

import base64
import csv
import hashlib
import io
import os
import subprocess
import sys
import tempfile
import tomllib
import unittest
import zipfile
from pathlib import Path

import swoon


PROJECT_ROOT = Path(__file__).resolve().parents[1]
BUILD_SCRIPT = PROJECT_ROOT / "scripts" / "build_wheel.py"
SMOKE_SCRIPT = PROJECT_ROOT / "scripts" / "smoke_wheel.py"
VERSION = swoon.__version__
WHEEL_VERSION = VERSION.replace("-", "_")
WHEEL_NAME = f"swoon_code-{WHEEL_VERSION}-py3-none-any.whl"
DIST_INFO = f"swoon_code-{WHEEL_VERSION}.dist-info"


class ConsumerReleaseTests(unittest.TestCase):
    def test_project_and_runtime_versions_match(self) -> None:
        configuration = tomllib.loads(
            (PROJECT_ROOT / "pyproject.toml").read_text(encoding="utf-8")
        )
        self.assertEqual(configuration["project"]["version"], swoon.__version__)

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
                self.assertFalse(any("cookies" in name.casefold() for name in names))
                self.assertFalse(any(name.startswith("tests/") for name in names))
                metadata = archive.read(f"{DIST_INFO}/METADATA").decode("utf-8")
                self.assertIn("Name: swoon-code\n", metadata)
                self.assertIn(f"Version: {VERSION}\n", metadata)
                self.assertIn("Requires-Dist: playwright>=1.50,<2\n", metadata)
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
