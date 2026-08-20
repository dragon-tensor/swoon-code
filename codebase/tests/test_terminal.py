from __future__ import annotations

import io
import unittest

from swoon.aeml.models import ResultStatus
from swoon.terminal import TerminalUI


class TTYBuffer(io.StringIO):
    def isatty(self) -> bool:
        return True


class TerminalUITests(unittest.TestCase):
    def test_plain_output_has_stable_semantic_prefixes(self) -> None:
        stdout = io.StringIO()
        stderr = io.StringIO()
        ui = TerminalUI(stdout=stdout, stderr=stderr, color=False)

        ui.agent("Direct answer")
        ui.plan("Inspect\nChange")
        ui.process("read-file — input/app.py")
        ui.process("run-tests — pytest", powerful=True)
        ui.result("run-tests — 12 passed", ResultStatus.SUCCESS)
        ui.result("run-tests — timed out", ResultStatus.TIMEOUT)
        ui.warning("Repairing malformed AEML")
        ui.error("Error [transport_failed]: unavailable")

        self.assertEqual(
            stdout.getvalue().splitlines(),
            [
                "[swoon-code] Direct answer",
                "-->> [plan] Inspect",
                "            Change",
                ">> read-file — input/app.py",
                ">> run-tests — pytest",
                ">> [success] run-tests — 12 passed",
                ">> [timeout] run-tests — timed out",
                "-->> [warning] Repairing malformed AEML",
            ],
        )
        self.assertEqual(
            stderr.getvalue(),
            "[swoon-code] Error [transport_failed]: unavailable\n",
        )
        self.assertEqual(ui.prompt(), "[user@swoon-code] ")

    def test_color_encodes_contrast_and_severity(self) -> None:
        stdout = TTYBuffer()
        stderr = TTYBuffer()
        ui = TerminalUI(stdout=stdout, stderr=stderr, color=True)

        ui.agent("answer")
        ui.plan("plan")
        ui.process("read")
        ui.process("execute", powerful=True)
        ui.result("done", ResultStatus.SUCCESS)
        ui.warning("careful")
        ui.error("failed")

        rendered = stdout.getvalue()
        self.assertIn("\033[1;97m[swoon-code] answer\033[0m", rendered)
        self.assertIn("\033[2;90m-->> [plan] plan\033[0m", rendered)
        self.assertIn("\033[1;90m>> read\033[0m", rendered)
        self.assertIn("\033[1;31m>> execute\033[0m", rendered)
        self.assertIn("\033[1;32m>> [success] done\033[0m", rendered)
        self.assertIn("\033[1;33m-->> [warning] careful\033[0m", rendered)
        self.assertIn("\033[1;31m[swoon-code] failed\033[0m", stderr.getvalue())
        self.assertEqual(
            ui.prompt(),
            "\033[1;97m[user@swoon-code] \033[0m",
        )

    def test_no_color_environment_disables_ansi_for_a_tty(self) -> None:
        ui = TerminalUI(stdout=TTYBuffer(), environment={"NO_COLOR": ""})

        self.assertFalse(ui.color)
        self.assertNotIn("\033", ui.prompt())


if __name__ == "__main__":
    unittest.main()
