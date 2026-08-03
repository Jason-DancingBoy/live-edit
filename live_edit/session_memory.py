"""Deprecated module — use live_edit.memory instead.

This module is kept for backward compatibility. All functionality has
been moved to `live_edit.memory`. Existing imports will continue to work
but will emit DeprecationWarning.
"""

import warnings

warnings.warn(
    "live_edit.session_memory is deprecated; use live_edit.memory instead",
    DeprecationWarning,
    stacklevel=2,
)

from .memory import (  # noqa: E402, F401
    KnowledgeBase,
    KnowledgeEntry,
    LongTermMemory,
    MemoryEntry,
    MemoryManager,
    ShortTermMemory,
)

# Keep the old name working
from .memory import LongTermMemory as SessionMemory  # noqa: E402, F401
