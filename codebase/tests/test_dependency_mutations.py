from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from swoon.aeml import AEMLParser, AEMLValidator
from swoon.aeml.errors import AEMLValidationError
from swoon.aeml.models import ProtocolError, Result, ResultStatus
from swoon.session import SessionManager
from swoon.tools import AgentToolDispatcher, ConfirmationRequest


class DependencyMutationToolTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.manager = SessionManager(self.root / "sessions")
        self.session = self.manager.create(session_id="sess_dependencies")
        self.output = self.session.paths.host_output
        self.dispatcher = AgentToolDispatcher(self.manager)
        self.parser = AEMLParser()
        self.validator = AEMLValidator(self.dispatcher.tool_specs)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def action(
        self,
        action_id: str,
        tool: str,
        manager: str,
        package: str,
        *,
        dev: bool = False,
    ):
        dev_xml = "<dev>true</dev>" if dev else ""
        message = self.parser.parse(
            f'<aeml turn="1" session="{self.session.id}">'
            f'<action id="{action_id}"><tool>{tool}</tool><args>'
            f"<manager>{manager}</manager><package>{package}</package>{dev_xml}"
            "</args><expect_confirm>true</expect_confirm></action>"
            "<next>await_result</next></aeml>"
        )
        return self.validator.validate(message).actions[0]

    def execute(
        self,
        action_id: str,
        tool: str,
        manager: str,
        package: str,
        *,
        dev: bool = False,
        confirmed: bool = True,
    ):
        return self.dispatcher.execute(
            self.action(action_id, tool, manager, package, dev=dev),
            self.session,
            confirmed=confirmed,
        )

    def test_pep621_install_requires_confirmation_and_never_installs_code(self) -> None:
        manifest = self.output / "pyproject.toml"
        manifest.write_text(
            '[project]\nname = "demo"\ndependencies = [\n    "requests==2.32.0",\n]\n',
            encoding="utf-8",
        )
        action = self.action(
            "pip_install",
            "install-dependency",
            "pip",
            "httpx==0.28.1",
            dev=True,
        )

        request = self.dispatcher.confirmation_request(action, self.session)
        blocked = self.dispatcher.execute(action, self.session)

        self.assertIsInstance(request, ConfirmationRequest)
        self.assertIn("downloads and package scripts stay disabled", request.reason)
        self.assertEqual(blocked.code, "confirmation_required")
        self.assertNotIn("httpx", manifest.read_text(encoding="utf-8"))

        approved = self.dispatcher.execute(action, self.session, confirmed=True)

        self.assertIsInstance(approved, Result)
        self.assertEqual(approved.status, ResultStatus.SUCCESS)
        self.assertIn("network=disabled", approved.body)
        self.assertIn("package_artifacts=not_installed", approved.body)
        updated = manifest.read_text(encoding="utf-8")
        self.assertIn("[project.optional-dependencies]", updated)
        self.assertIn('dev = [\n    "httpx==0.28.1",\n]', updated)

        removed = self.execute(
            "pip_remove",
            "remove-dependency",
            "pip",
            "httpx",
        )
        self.assertIsInstance(removed, Result)
        self.assertNotIn("httpx", manifest.read_text(encoding="utf-8"))

    def test_confirmation_guard_detects_manifest_change(self) -> None:
        manifest = self.output / "requirements.txt"
        manifest.write_text("requests==2.32.0\n", encoding="utf-8")
        action = self.action(
            "guarded_install",
            "install-dependency",
            "pip",
            "httpx==0.28.1",
        )
        request = self.dispatcher.confirmation_request(action, self.session)
        self.assertIsInstance(request, ConfirmationRequest)
        self.manager.reserve_action_ids(self.session, (action.source.id,))
        self.manager.request_confirmation(
            self.session,
            action.source,
            request.reason,
            request.guard,
        )
        manifest.write_text("requests==2.32.1\n", encoding="utf-8")

        response = self.dispatcher.execute(action, self.session, confirmed=True)

        self.assertEqual(response.code, "confirmation_stale")
        self.assertNotIn("httpx", manifest.read_text(encoding="utf-8"))

    def test_exact_registry_declarations_are_required(self) -> None:
        (self.output / "requirements.txt").write_text("", encoding="utf-8")
        (self.output / "package.json").write_text("{}\n", encoding="utf-8")
        invalid = (
            ("pip", "requests>=2"),
            ("pip", "https://example.test/pkg.whl"),
            ("npm", "react@latest"),
            ("npm", "../local-package@1.0.0"),
        )
        for index, (manager, package) in enumerate(invalid, start=1):
            with self.subTest(manager=manager, package=package):
                action = self.action(
                    f"invalid_{index}",
                    "install-dependency",
                    manager,
                    package,
                )
                response = self.dispatcher.confirmation_request(action, self.session)
                self.assertIsInstance(response, ProtocolError)
                self.assertEqual(response.code, "invalid_dependency")

    def test_schema_requires_package_and_declared_confirmation(self) -> None:
        missing_package = self.parser.parse(
            f'<aeml turn="1" session="{self.session.id}">'
            '<action id="missing_package"><tool>install-dependency</tool>'
            "<args><manager>pip</manager></args><expect_confirm>true</expect_confirm>"
            "</action><next>await_result</next></aeml>"
        )
        missing_confirmation = self.parser.parse(
            f'<aeml turn="1" session="{self.session.id}">'
            '<action id="missing_confirmation"><tool>install-dependency</tool>'
            "<args><manager>pip</manager><package>httpx==0.28.1</package></args>"
            "</action><next>await_result</next></aeml>"
        )

        with self.assertRaises(AEMLValidationError) as package_error:
            self.validator.validate(missing_package)
        with self.assertRaises(AEMLValidationError) as confirmation_error:
            self.validator.validate(missing_confirmation)

        self.assertEqual(package_error.exception.code, "missing_argument")
        self.assertEqual(confirmation_error.exception.code, "confirmation_required")

    def test_complex_requirements_removal_fails_without_partial_edit(self) -> None:
        manifest = self.output / "requirements.txt"
        original = "requests==2.32.0 \\\n    --hash=sha256:abc\n"
        manifest.write_text(original, encoding="utf-8")
        action = self.action(
            "remove_hashed",
            "remove-dependency",
            "pip",
            "requests",
        )

        response = self.dispatcher.confirmation_request(action, self.session)

        self.assertEqual(response.code, "manifest_unsupported")
        self.assertEqual(manifest.read_text(encoding="utf-8"), original)

    def test_lockfile_refuses_manifest_change(self) -> None:
        manifest = self.output / "package.json"
        manifest.write_text('{"dependencies": {}}\n', encoding="utf-8")
        (self.output / "package-lock.json").write_text("{}\n", encoding="utf-8")
        action = self.action(
            "locked_npm",
            "install-dependency",
            "npm",
            "react@19.0.0",
        )

        response = self.dispatcher.confirmation_request(action, self.session)

        self.assertEqual(response.code, "lockfile_present")
        self.assertNotIn("react", manifest.read_text(encoding="utf-8"))

    def test_javascript_and_composer_json_changes_are_structured(self) -> None:
        package_json = self.output / "package.json"
        package_json.write_text(
            '{"name":"demo","scripts":{"postinstall":"touch SHOULD_NOT_EXIST"}}\n',
            encoding="utf-8",
        )
        installed = self.execute(
            "npm_install",
            "install-dependency",
            "npm",
            "@types/node@22.0.0",
            dev=True,
        )
        self.assertIsInstance(installed, Result)
        self.assertIn('"@types/node": "22.0.0"', package_json.read_text(encoding="utf-8"))
        self.assertFalse((self.output / "SHOULD_NOT_EXIST").exists())
        removed = self.execute(
            "npm_remove",
            "remove-dependency",
            "npm",
            "@types/node",
        )
        self.assertIsInstance(removed, Result)
        self.assertNotIn("@types/node", package_json.read_text(encoding="utf-8"))

        composer = self.output / "composer.json"
        composer.write_text('{"name":"demo/app"}\n', encoding="utf-8")
        added = self.execute(
            "composer_install",
            "install-dependency",
            "composer",
            "monolog/monolog@3.8.0",
        )
        self.assertIsInstance(added, Result)
        self.assertIn('"monolog/monolog": "3.8.0"', composer.read_text(encoding="utf-8"))
        deleted = self.execute(
            "composer_remove",
            "remove-dependency",
            "composer",
            "monolog/monolog",
        )
        self.assertIsInstance(deleted, Result)

    def test_cargo_go_and_bundler_manifests_are_supported(self) -> None:
        cargo = self.output / "Cargo.toml"
        cargo.write_text(
            '[package]\nname = "demo"\nversion = "0.1.0"\n',
            encoding="utf-8",
        )
        cargo_add = self.execute(
            "cargo_install",
            "install-dependency",
            "cargo",
            "serde@1.0.200",
        )
        self.assertIsInstance(cargo_add, Result)
        self.assertIn('"serde" = "=1.0.200"', cargo.read_text(encoding="utf-8"))
        cargo_remove = self.execute(
            "cargo_remove",
            "remove-dependency",
            "cargo",
            "serde",
        )
        self.assertIsInstance(cargo_remove, Result)

        go_mod = self.output / "go.mod"
        go_mod.write_text("module example.test/demo\n\ngo 1.23\n", encoding="utf-8")
        go_add = self.execute(
            "go_install",
            "install-dependency",
            "go",
            "golang.org/x/text@v0.22.0",
        )
        self.assertIsInstance(go_add, Result)
        self.assertIn(
            "require golang.org/x/text v0.22.0",
            go_mod.read_text(encoding="utf-8"),
        )
        go_remove = self.execute(
            "go_remove",
            "remove-dependency",
            "go",
            "golang.org/x/text",
        )
        self.assertIsInstance(go_remove, Result)

        gemfile = self.output / "Gemfile"
        gemfile.write_text('source "https://rubygems.org"\n', encoding="utf-8")
        gem_add = self.execute(
            "gem_install",
            "install-dependency",
            "bundler",
            "rack@3.1.0",
            dev=True,
        )
        self.assertIsInstance(gem_add, Result)
        self.assertIn('gem "rack", "= 3.1.0"', gemfile.read_text(encoding="utf-8"))
        gem_remove = self.execute(
            "gem_remove",
            "remove-dependency",
            "bundler",
            "rack",
        )
        self.assertIsInstance(gem_remove, Result)

    def test_unfinished_output_chunk_blocks_dependency_change(self) -> None:
        (self.output / "requirements.txt").write_text("", encoding="utf-8")
        partial = self.parser.parse(
            f'<aeml turn="1" session="{self.session.id}">'
            '<action id="partial"><tool>create-file</tool><path>partial.txt</path>'
            '<args><content>part</content></args><chunk seq="1" final="false"/>'
            "</action><next>await_result</next></aeml>"
        )
        partial_action = self.validator.validate(partial).actions[0]
        self.assertIsInstance(
            self.dispatcher.execute(partial_action, self.session),
            Result,
        )
        dependency = self.action(
            "blocked_dependency",
            "install-dependency",
            "pip",
            "httpx==0.28.1",
        )

        response = self.dispatcher.confirmation_request(dependency, self.session)

        self.assertEqual(response.code, "write_incomplete")


if __name__ == "__main__":
    unittest.main()
