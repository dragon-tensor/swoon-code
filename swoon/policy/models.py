"""Typed authorization results produced by the path-policy boundary."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from pathlib import Path

from swoon.aeml.models import PathRef


class PathAccess(str, Enum):
    READ = "read"
    WRITE = "write"


class PathExistence(str, Enum):
    MUST_EXIST = "must_exist"
    MAY_EXIST = "may_exist"
    MUST_NOT_EXIST = "must_not_exist"


class PathKind(str, Enum):
    ANY = "any"
    FILE = "file"
    DIRECTORY = "directory"


@dataclass(frozen=True, slots=True)
class ComponentFingerprint:
    relative_parts: tuple[str, ...]
    device: int
    inode: int
    file_type: int


@dataclass(frozen=True, slots=True)
class ResolvedPath:
    reference: PathRef
    virtual_path: str
    host_path: Path
    host_root: Path
    access: PathAccess
    existence: PathExistence
    kind: PathKind
    allow_root: bool
    exists: bool
    fingerprints: tuple[ComponentFingerprint, ...]
