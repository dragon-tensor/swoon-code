"""Swoon Code's browser transport and AEML protocol engine."""

from .aeml import AEMLParser, AEMLValidator
from .policy import PathPolicy
from .session import SessionManager

__all__ = ["AEMLParser", "AEMLValidator", "PathPolicy", "SessionManager"]
__version__ = "0.1.0"
