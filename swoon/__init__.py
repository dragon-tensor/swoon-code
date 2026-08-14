"""Swoon Code's browser transport and AEML protocol engine."""

from .aeml import (
    AEMLContextBuilder,
    AEMLContextRenderer,
    AEMLParser,
    AEMLPromptBuilder,
    AEMLValidator,
    ContextLimits,
)
from .policy import PathPolicy
from .session import SessionManager
from .tools import ReadOnlyToolDispatcher

__all__ = [
    "AEMLContextBuilder",
    "AEMLContextRenderer",
    "AEMLParser",
    "AEMLPromptBuilder",
    "AEMLValidator",
    "ContextLimits",
    "PathPolicy",
    "ReadOnlyToolDispatcher",
    "SessionManager",
]
__version__ = "0.1.0"
