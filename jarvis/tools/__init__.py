"""Importing this package registers every tool on the shared registry."""

# Side-effect imports: each module decorates its tools onto `registry`.
from . import (
    custom_brain,  # noqa: F401,E402
    delegate,  # noqa: F401,E402
    gemini_agent,  # noqa: F401,E402
    recall,  # noqa: F401,E402
    system,  # noqa: F401,E402
    telegram,  # noqa: F401,E402
)
from .base import Tool, ToolRegistry, registry  # noqa: F401

__all__ = ["registry", "Tool", "ToolRegistry"]
