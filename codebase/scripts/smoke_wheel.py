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
        session_help = _run(console, "session", "--help", environment=command_environment)
        if not all(action in session_help for action in ("list", "show", "export", "delete")):
            raise RuntimeError("Installed session-management help is incomplete")
        _run(
            python,
            "-c",
            f"import swoon; assert swoon.__version__ == {expected_version!r}",
            environment=command_environment,
        )

        session_root = temporary / "sessions"
        exported = temporary / "exported"
        _run(
            python,
            "-c",
            (
                "from pathlib import Path; "
                "from swoon.session import SessionManager, SessionStatus; "
                f"m=SessionManager(Path({str(session_root)!r})); "
                "s=m.create(session_id='sess_wheel_smoke'); "
                "(s.paths.host_output/'result.txt').write_text('installed\\n',encoding='utf-8'); "
                "m.set_status(s,SessionStatus.COMPLETED)"
            ),
            environment=command_environment,
        )
        listed = _run(
            console,
            "session",
            "list",
            "--session-dir",
            str(session_root),
            environment=command_environment,
        )
        if "sess_wheel_smoke\tcompleted" not in listed:
            raise RuntimeError("Installed CLI did not list its consumer smoke session")
        shown = _run(
            console,
            "session",
            "show",
            "sess_wheel_smoke",
            "--session-dir",
            str(session_root),
            environment=command_environment,
        )
        if "Status: completed" not in shown:
            raise RuntimeError("Installed CLI did not inspect its consumer smoke session")
        _run(
            console,
            "session",
            "export",
            "sess_wheel_smoke",
            str(exported),
            "--session-dir",
            str(session_root),
            environment=command_environment,
        )
        if (exported / "result.txt").read_text(encoding="utf-8") != "installed\n":
            raise RuntimeError("Installed CLI session export produced unexpected output")
        _run(
            console,
            "session",
            "delete",
            "sess_wheel_smoke",
            "--yes",
            "--session-dir",
            str(session_root),
            environment=command_environment,
        )
        if (session_root / "sess_wheel_smoke").exists():
            raise RuntimeError("Installed CLI session deletion did not complete")
        _run(
            python,
            "-c",
            (
                "from importlib.metadata import files, metadata; "
                "m=metadata('swoon-code'); "
                "assert m['License-Expression']=='Apache-2.0'; "
                "assert set(m.get_all('License-File'))=={'LICENSE','NOTICE'}; "
                "p={str(item) for item in files('swoon-code')}; "
                "assert any(item.endswith('.dist-info/licenses/LICENSE') for item in p); "
                "assert any(item.endswith('.dist-info/licenses/NOTICE') for item in p)"
            ),
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
    parser.add_argument(
        "wheel",
        nargs="?",
        type=Path,
        help="existing wheel; builds one if omitted",
    )
    args = parser.parse_args(argv)
    smoke_wheel(args.wheel)
    print("Consumer wheel smoke test passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
