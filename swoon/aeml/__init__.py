"""Public API for building, rendering, parsing, and validating AEML."""

from .context import AEMLContextBuilder, AEMLContextRenderer, ContextLimits
from .errors import (
    AEMLChannelError,
    AEMLContextError,
    AEMLError,
    AEMLParseError,
    AEMLTruncatedError,
    AEMLValidationError,
)
from .models import (
    AEMLContext,
    AEMLMessage,
    Action,
    Argument,
    Chunk,
    Environment,
    NextDirective,
    PathRef,
    ProtocolError,
    Result,
    ResultStatus,
    ResultSummary,
    Root,
    SystemNotice,
    Truncation,
    ValidatedAction,
    ValidatedMessage,
)
from .parser import AEMLParser
from .prompts import AEMLPromptBuilder
from .tool_registry import TOOL_SPECS, get_tool_spec, tool_names
from .validator import AEMLValidator

__all__ = [
    "AEMLChannelError",
    "AEMLContext",
    "AEMLContextBuilder",
    "AEMLContextError",
    "AEMLContextRenderer",
    "AEMLError",
    "AEMLMessage",
    "AEMLParseError",
    "AEMLParser",
    "AEMLPromptBuilder",
    "AEMLTruncatedError",
    "AEMLValidationError",
    "AEMLValidator",
    "Action",
    "Argument",
    "Chunk",
    "ContextLimits",
    "Environment",
    "NextDirective",
    "PathRef",
    "ProtocolError",
    "Result",
    "ResultStatus",
    "ResultSummary",
    "Root",
    "SystemNotice",
    "TOOL_SPECS",
    "Truncation",
    "ValidatedAction",
    "ValidatedMessage",
    "get_tool_spec",
    "tool_names",
]
