"""Narrow, platform-owned capabilities available to generated plugins."""

from self_grow_agent.capabilities.mysql_replication import (
    rebuild_replication,
    rebuild_replication_from_message,
)

__all__ = ["rebuild_replication", "rebuild_replication_from_message"]
