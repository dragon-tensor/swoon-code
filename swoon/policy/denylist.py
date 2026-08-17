"""Non-removable credential-shaped path exclusions."""

from __future__ import annotations

from fnmatch import fnmatchcase


DEFAULT_EXACT_PATHS = frozenset(
    {
        ".git/config",
        ".git/credentials",
        ".git-credentials",
    }
)
DEFAULT_PROTECTED_DIRECTORIES = frozenset(
    {
        ".ssh",
        ".gnupg",
        ".aws",
        ".azure",
        ".kube",
        ".config/gcloud",
    }
)
DEFAULT_FILENAME_PATTERNS = (
    ".swoon-tmp-*",
    ".swoon-stage-*",
    ".env",
    ".env.*",
    ".netrc",
    ".npmrc",
    ".pypirc",
    "*.pem",
    "*.key",
    "id_rsa*",
    "id_dsa*",
    "id_ecdsa*",
    "id_ed25519*",
    "*credentials*.json",
    "*secret*.json",
    "service-account*.json",
)


class CredentialDenylist:
    """Match protected paths case-insensitively on every host OS.

    Callers may add exclusions, but the protocol's defaults cannot be removed.
    """

    def __init__(
        self,
        *,
        exact_paths: tuple[str, ...] = (),
        protected_directories: tuple[str, ...] = (),
        filename_patterns: tuple[str, ...] = (),
    ) -> None:
        self.exact_paths = DEFAULT_EXACT_PATHS | {
            item.strip("/").casefold() for item in exact_paths
        }
        self.protected_directories = DEFAULT_PROTECTED_DIRECTORIES | {
            item.strip("/").casefold() for item in protected_directories
        }
        self.filename_patterns = DEFAULT_FILENAME_PATTERNS + tuple(
            item.casefold() for item in filename_patterns
        )

    def denies(self, parts: tuple[str, ...]) -> bool:
        if not parts:
            return False
        folded_parts = tuple(part.casefold() for part in parts)
        relative = "/".join(folded_parts)
        if relative in self.exact_paths:
            return True
        if any(
            relative == directory or relative.startswith(directory + "/")
            for directory in self.protected_directories
        ):
            return True
        basename = folded_parts[-1]
        return any(fnmatchcase(basename, pattern) for pattern in self.filename_patterns)
