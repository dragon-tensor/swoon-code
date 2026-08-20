from __future__ import annotations

import os
import shutil
import subprocess
import unittest
from pathlib import Path


CODEBASE_ROOT = Path(__file__).resolve().parents[1]
REPOSITORY_ROOT = CODEBASE_ROOT.parent
SETUP_ROOT = REPOSITORY_ROOT / "setup"


class ConsumerLayoutTests(unittest.TestCase):
    def test_repository_has_three_visible_product_roots(self) -> None:
        visible_directories = {
            item.name
            for item in REPOSITORY_ROOT.iterdir()
            if item.is_dir() and not item.name.startswith(".")
        }
        self.assertEqual(visible_directories, {"codebase", "setup", "work"})
        self.assertTrue((CODEBASE_ROOT / "pyproject.toml").is_file())
        self.assertEqual(
            {
                item.name
                for item in (REPOSITORY_ROOT / "work").iterdir()
                if item.is_dir() and not item.name.startswith(".")
            },
            {"input", "output"},
        )

    def test_setup_covers_linux_variants_macos_windows_and_dev(self) -> None:
        required = {
            "install.sh",
            "common/install-unix.sh",
            "linux/install.sh",
            "linux/arch.sh",
            "linux/debian-ubuntu.sh",
            "linux/fedora.sh",
            "macos/install.sh",
            "windows/install.ps1",
            "windows/install.cmd",
            "dev/start-headed.sh",
            "dev/start-headed.ps1",
        }
        self.assertEqual(
            {str(path.relative_to(SETUP_ROOT)) for path in SETUP_ROOT.rglob("*") if path.is_file()}
            & required,
            required,
        )

    def test_unix_setup_scripts_parse_and_only_dev_launcher_is_headed(self) -> None:
        shell = shutil.which("sh")
        scripts = tuple(sorted(SETUP_ROOT.rglob("*.sh")))
        if shell is not None:
            subprocess.run(
                [shell, "-n", *(str(path) for path in scripts)],
                check=True,
                capture_output=True,
                text=True,
            )
        if os.name != "nt":
            self.assertTrue(all(path.stat().st_mode & 0o111 for path in scripts))

        consumer_text = "\n".join(
            path.read_text(encoding="utf-8")
            for path in scripts
            if "dev" not in path.relative_to(SETUP_ROOT).parts
        )
        self.assertNotIn("--headed", consumer_text)
        self.assertIn(
            "--headed",
            (SETUP_ROOT / "dev" / "start-headed.sh").read_text(encoding="utf-8"),
        )


if __name__ == "__main__":
    unittest.main()
