"""ReconnectStrategy - Exponential backoff with jitter."""
from __future__ import annotations

import random
from dataclasses import dataclass


@dataclass
class ExponentialBackoff:
    """Exponential backoff with jitter for reconnection."""

    initial_delay_ms: int = 1000
    max_delay_ms: int = 30_000
    multiplier: float = 2.0
    jitter_factor: float = 0.1
    max_retries: int | None = None

    def next_delay(self, attempt: int) -> float:
        """Calculate next delay in milliseconds."""
        delay = min(
            self.initial_delay_ms * (self.multiplier ** attempt),
            self.max_delay_ms,
        )
        jitter = delay * self.jitter_factor * (2 * random.random() - 1)
        return max(0, delay + jitter)

    def should_retry(self, attempt: int) -> bool:
        """Check if another retry is allowed."""
        if self.max_retries is None:
            return True
        return attempt < self.max_retries
