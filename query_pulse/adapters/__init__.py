"""Database adapters, shared by the API (test connection, capability checks) and, from Phase 3,
the collector.

The shared contract is deliberately thin: connect, discover the version, check capabilities.
Everything else is engine-specific; MongoDB operations are never presented as SQL.
"""

from query_pulse.adapters.base import (
    Adapter,
    AdapterError,
    Capability,
    ConnectionParams,
    ErrorCode,
    ProbeResult,
    TlsMode,
)
from query_pulse.adapters.registry import adapter_for

__all__ = [
    "Adapter",
    "AdapterError",
    "Capability",
    "ConnectionParams",
    "ErrorCode",
    "ProbeResult",
    "TlsMode",
    "adapter_for",
]
