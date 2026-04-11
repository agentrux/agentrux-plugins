"""Deduplicator - event_id based duplicate detection with LRU eviction."""
from __future__ import annotations

import uuid
from collections import OrderedDict

from agentrux.sdk.stats import DeduplicatorStats


class Deduplicator:
    """LRU-based duplicate detector using event_id."""

    def __init__(self, capacity: int = 10_000):
        if capacity <= 0:
            raise ValueError("capacity must be positive")
        self._seen: OrderedDict[str, None] = OrderedDict()
        self._capacity = capacity
        self._total_seen = 0
        self._duplicates_dropped = 0

    def is_duplicate(self, event_id: str) -> bool:
        """Check if event_id has been seen. Returns True if duplicate."""
        if not self._validate_uuid(event_id):
            return False  # Invalid IDs pass through (caught elsewhere)

        if event_id in self._seen:
            self._duplicates_dropped += 1
            # Move to end (most recent)
            self._seen.move_to_end(event_id)
            return True
        return False

    def mark_seen(self, event_id: str) -> None:
        """Mark event_id as processed. Evicts oldest on capacity overflow."""
        if not self._validate_uuid(event_id):
            return

        self._seen[event_id] = None
        self._seen.move_to_end(event_id)
        self._total_seen += 1

        while len(self._seen) > self._capacity:
            self._seen.popitem(last=False)

    def reset(self) -> None:
        """Clear all state."""
        self._seen.clear()
        self._total_seen = 0
        self._duplicates_dropped = 0

    @staticmethod
    def _validate_uuid(event_id: str) -> bool:
        """Validate UUID format to prevent cache pollution."""
        try:
            uuid.UUID(event_id)
            return True
        except (ValueError, AttributeError):
            return False

    @property
    def stats(self) -> DeduplicatorStats:
        return DeduplicatorStats(
            total_seen=self._total_seen,
            duplicates_dropped=self._duplicates_dropped,
            current_size=len(self._seen),
        )
