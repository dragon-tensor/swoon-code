"""Swoon Code's browser transport and AEML protocol engine."""

from .aeml import (
    AEMLContextBuilder,
    AEMLContextRenderer,
    AEMLParser,
    AEMLPromptBuilder,
    AEMLValidator,
    ContextLimits,
)
from .orchestration import (
    AEMLOrchestrator,
    AgentOrchestrator,
    OrchestrationError,
    OrchestrationLimits,
    ReadOnlyOrchestrator,
    RunResult,
    RunStopReason,
)
from .policy import PathPolicy
from .session import SessionManager
from .tools import AgentToolDispatcher, ReadOnlyToolDispatcher

__all__ = [
    "AEMLContextBuilder",
    "AEMLContextRenderer",
    "AEMLOrchestrator",
    "AEMLParser",
    "AEMLPromptBuilder",
    "AEMLValidator",
    "AgentOrchestrator",
    "AgentToolDispatcher",
    "ContextLimits",
    "OrchestrationError",
    "OrchestrationLimits",
    "PathPolicy",
    "ReadOnlyToolDispatcher",
    "ReadOnlyOrchestrator",
    "RunResult",
    "RunStopReason",
    "SessionManager",
]
__version__ = "0.1.0"
