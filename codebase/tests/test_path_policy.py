from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path

from swoon.aeml.models import PathRef, Root
from swoon.policy import (
    CredentialDenylist,
    PathAccess,
    PathExistence,
    PathKind,
    PathPolicy,
    PathPolicyError,
)
from swoon.session import SessionManager


class PathPolicyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        source = self.root / "project"
        (source / "src").mkdir(parents=True)
        (source / ".git").mkdir()
        (source / "src" / "app.py").write_text("print('hello')\n", encoding="utf-8")
        (source / ".env").write_text("TOKEN=secret\n", encoding="utf-8")
        (source / ".git" / "config").write_text("[remote]\n", encoding="utf-8")
        self.manager = SessionManager(self.root / "sessions")
        self.session = self.manager.create(source, session_id="sess_policy")
        self.policy = PathPolicy(self.session.paths)

    def tearDown(self) -> None:
        self._make_writable(self.root)
        self.temporary.cleanup()

    @staticmethod
    def _make_writable(root: Path) -> None:
        if not root.exists():
            return
        for directory, directories, files in os.walk(root, topdown=False, followlinks=False):
            directory_path = Path(directory)
            for name in files:
                path = directory_path / name
                if not path.is_symlink():
                    try:
                        path.chmod(0o600)
                    except OSError:
                        pass
            for name in directories:
                path = directory_path / name
                if not path.is_symlink():
                    try:
                        path.chmod(0o700)
                    except OSError:
                        pass
            try:
                directory_path.chmod(0o700)
            except OSError:
                pass

    def assert_code(self, code: str, reference: PathRef, **kwargs) -> PathPolicyError:
        with self.assertRaises(PathPolicyError) as raised:
            self.policy.resolve(reference, **kwargs)
        self.assertEqual(raised.exception.code, code)
        return raised.exception

    def test_resolves_input_file_to_private_host_path(self) -> None:
        resolved = self.policy.resolve(
            PathRef("src/app.py", Root.INPUT),
            kind=PathKind.FILE,
        )

        self.assertEqual(resolved.virtual_path, "/input/sess_policy/src/app.py")
        self.assertEqual(resolved.host_path, self.session.paths.host_input / "src" / "app.py")
        self.assertTrue(resolved.exists)
        self.assertGreaterEqual(len(resolved.fingerprints), 3)

    def test_root_read_is_allowed_but_root_write_requires_explicit_permission(self) -> None:
        root = self.policy.resolve(
            PathRef(".", Root.OUTPUT),
            kind=PathKind.DIRECTORY,
        )
        self.assertEqual(root.host_path, self.session.paths.host_output)

        self.assert_code(
            "path_escape",
            PathRef(".", Root.OUTPUT),
            access=PathAccess.WRITE,
            kind=PathKind.DIRECTORY,
        )
        writable_root = self.policy.resolve(
            PathRef(".", Root.OUTPUT),
            access=PathAccess.WRITE,
            kind=PathKind.DIRECTORY,
            allow_root=True,
        )
        self.assertEqual(writable_root.host_path, self.session.paths.host_output)

    def test_input_write_is_rejected_even_when_target_does_not_exist(self) -> None:
        self.assert_code(
            "input_readonly",
            PathRef("new.py", Root.INPUT),
            access=PathAccess.WRITE,
            existence=PathExistence.MUST_NOT_EXIST,
        )

    def test_absolute_traversal_and_nonportable_paths_are_rejected(self) -> None:
        cases = {
            "/etc/passwd": "path_escape",
            "../outside": "path_escape",
            "src/../outside": "path_escape",
            r"C:\Windows\system.ini": "path_escape",
            r"\\server\share": "path_escape",
            r"src\app.py": "path_escape",
            "src//app.py": "invalid_path",
            "src/./app.py": "invalid_path",
            "NUL.txt": "invalid_path",
            "bad?.txt": "invalid_path",
            "trailing.": "invalid_path",
            "zero\x00byte": "path_escape",
        }
        for value, code in cases.items():
            with self.subTest(value=value):
                self.assert_code(
                    code,
                    PathRef(value, Root.OUTPUT),
                    existence=PathExistence.MAY_EXIST,
                )

    def test_create_target_requires_existing_parent_and_missing_final_path(self) -> None:
        resolved = self.policy.resolve(
            PathRef("new.py", Root.OUTPUT),
            access=PathAccess.WRITE,
            existence=PathExistence.MUST_NOT_EXIST,
            kind=PathKind.FILE,
        )
        self.assertFalse(resolved.exists)

        self.assert_code(
            "path_not_found",
            PathRef("missing/new.py", Root.OUTPUT),
            access=PathAccess.WRITE,
            existence=PathExistence.MUST_NOT_EXIST,
        )

        existing = self.session.paths.host_output / "existing.py"
        existing.write_text("pass\n", encoding="utf-8")
        self.assert_code(
            "path_exists",
            PathRef("existing.py", Root.OUTPUT),
            access=PathAccess.WRITE,
            existence=PathExistence.MUST_NOT_EXIST,
        )

    def test_existence_and_kind_are_enforced(self) -> None:
        directory = self.session.paths.host_output / "folder"
        directory.mkdir()
        file = self.session.paths.host_output / "file.txt"
        file.write_text("x", encoding="utf-8")

        self.assert_code(
            "not_file",
            PathRef("folder", Root.OUTPUT),
            kind=PathKind.FILE,
        )
        self.assert_code(
            "not_directory",
            PathRef("file.txt", Root.OUTPUT),
            kind=PathKind.DIRECTORY,
        )
        self.assert_code("path_not_found", PathRef("absent", Root.OUTPUT))

    @unittest.skipUnless(hasattr(os, "symlink"), "symbolic links unavailable")
    def test_symlink_components_and_targets_are_rejected(self) -> None:
        outside = self.root / "outside"
        outside.mkdir()
        (outside / "secret").write_text("secret", encoding="utf-8")
        (self.session.paths.host_output / "link").symlink_to(outside, target_is_directory=True)

        self.assert_code("path_escape", PathRef("link/secret", Root.OUTPUT))
        self.assert_code("path_escape", PathRef("link", Root.OUTPUT))

    @unittest.skipUnless(hasattr(os, "link"), "hard links unavailable")
    def test_hard_linked_files_are_rejected(self) -> None:
        outside = self.root / "outside.txt"
        outside.write_text("secret", encoding="utf-8")
        os.link(outside, self.session.paths.host_output / "hard.txt")

        self.assert_code("path_escape", PathRef("hard.txt", Root.OUTPUT), kind=PathKind.FILE)

    def test_credential_paths_are_denied_before_filesystem_access(self) -> None:
        for reference in (
            PathRef(".env", Root.INPUT),
            PathRef(".git/config", Root.INPUT),
            PathRef("SERVER.PEM", Root.OUTPUT),
            PathRef(".aws/credentials", Root.OUTPUT),
        ):
            with self.subTest(reference=reference):
                self.assert_code(
                    "credential_path",
                    reference,
                    existence=PathExistence.MAY_EXIST,
                )

    def test_custom_denylist_can_only_add_protected_names(self) -> None:
        policy = PathPolicy(
            self.session.paths,
            denylist=CredentialDenylist(filename_patterns=("private-*",)),
        )
        with self.assertRaises(PathPolicyError) as custom:
            policy.resolve(
                PathRef("private-notes", Root.OUTPUT),
                existence=PathExistence.MAY_EXIST,
            )
        self.assertEqual(custom.exception.code, "credential_path")
        with self.assertRaises(PathPolicyError) as default:
            policy.resolve(PathRef(".env", Root.INPUT))
        self.assertEqual(default.exception.code, "credential_path")

    def test_directory_listing_filter_hides_denied_children(self) -> None:
        directory = self.policy.resolve(
            PathRef(".", Root.INPUT),
            kind=PathKind.DIRECTORY,
        )
        visible = self.policy.visible_child_names(
            directory,
            ["src", ".env", ".git", "server.pem"],
        )
        self.assertEqual(visible, ("src", ".git"))

        git_directory = self.policy.resolve(
            PathRef(".git", Root.INPUT),
            kind=PathKind.DIRECTORY,
        )
        self.assertEqual(
            self.policy.visible_child_names(git_directory, ["HEAD", "config"]),
            ("HEAD",),
        )

    def test_revalidation_detects_atomic_file_replacement(self) -> None:
        original = self.session.paths.host_output / "app.py"
        original.write_text("one", encoding="utf-8")
        resolved = self.policy.resolve(PathRef("app.py", Root.OUTPUT), kind=PathKind.FILE)
        replacement = self.session.paths.host_output / "replacement"
        replacement.write_text("two", encoding="utf-8")
        replacement.replace(original)

        with self.assertRaises(PathPolicyError) as raised:
            self.policy.revalidate(resolved)
        self.assertEqual(raised.exception.code, "path_changed")

    def test_revalidation_detects_new_create_target(self) -> None:
        resolved = self.policy.resolve(
            PathRef("new.txt", Root.OUTPUT),
            access=PathAccess.WRITE,
            existence=PathExistence.MUST_NOT_EXIST,
            kind=PathKind.FILE,
        )
        (self.session.paths.host_output / "new.txt").write_text("raced", encoding="utf-8")

        with self.assertRaises(PathPolicyError) as raised:
            self.policy.revalidate(resolved)
        self.assertEqual(raised.exception.code, "path_changed")

    def test_explicit_cross_session_forms_cannot_resolve(self) -> None:
        for value in ("/input/sess_other/file", "../sess_other/file"):
            with self.subTest(value=value):
                self.assert_code(
                    "path_escape",
                    PathRef(value, Root.INPUT),
                    existence=PathExistence.MAY_EXIST,
                )


if __name__ == "__main__":
    unittest.main()
