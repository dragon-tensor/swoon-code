#!/usr/bin/env python3
"""Install a Swoon wheel without network access and smoke-test its CLI."""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
import tempfile
import venv
from pathlib import Path

from build_wheel import PROJECT_ROOT, build_wheel, project_version


def smoke_wheel(wheel: Path | None = None) -> None:
    expected_version = project_version(PROJECT_ROOT)
    with tempfile.TemporaryDirectory(prefix="swoon-consumer-smoke-") as directory:
        temporary = Path(directory)
        selected = wheel.resolve(strict=True) if wheel is not None else build_wheel(
            PROJECT_ROOT,
            temporary / "dist",
        )
        environment = temporary / "venv"
        venv.EnvBuilder(with_pip=True, clear=False).create(environment)
        python = environment / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
        console = environment / ("Scripts/swoon.exe" if os.name == "nt" else "bin/swoon")
        command_environment = dict(os.environ)
        command_environment.update(
            {
                "PIP_DISABLE_PIP_VERSION_CHECK": "1",
                "PIP_NO_CACHE_DIR": "1",
                "PIP_NO_INDEX": "1",
                "PYTHONDONTWRITEBYTECODE": "1",
            }
        )
        _run(
            python,
            "-m",
            "pip",
            "install",
            "--no-compile",
            "--no-deps",
            str(selected),
            environment=command_environment,
        )
        version = _run(console, "--version", environment=command_environment).strip()
        if version != f"swoon {expected_version}":
            raise RuntimeError(f"Unexpected installed version output: {version!r}")
        help_text = _run(console, "--help", environment=command_environment)
        if "doctor" not in help_text or "bounded AEML coding agent" not in help_text:
            raise RuntimeError("Installed root help is missing consumer commands")
        doctor_help = _run(console, "doctor", "--help", environment=command_environment)
        if "--launch-browser" not in doctor_help:
            raise RuntimeError("Installed doctor help is incomplete")
        _run(
            python,
            "-c",
            f"import swoon; assert swoon.__version__ == {expected_version!r}",
            environment=command_environment,
        )


def _run(
    executable: Path,
    *arguments: str,
    environment: dict[str, str],
) -> str:
    completed = subprocess.run(
        [str(executable), *arguments],
        check=False,
        capture_output=True,
        text=True,
        timeout=120,
        env=environment,
    )
    if completed.returncode != 0:
        raise RuntimeError(
            f"Consumer command failed ({completed.returncode}): "
            f"{completed.stderr.strip() or completed.stdout.strip()}"
        )
    return completed.stdout


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("wheel", nargs="?", type=Path, help="existing wheel; builds one if omitted")
    args = parser.parse_args(argv)
    smoke_wheel(args.wheel)
    print("Consumer wheel smoke test passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
