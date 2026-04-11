"""ReorderBuffer - Sequence-based message reordering with timeout."""
from __future__ import annotations

import time
from sortedcontainers import SortedDict

from agentrux.sdk.envelope import MessageEnvelope
from agentrux.sdk.stats import ReorderBufferStats


class ReorderBuffer:
    """Reorders messages by sequence number within a sliding window.

    Messages are buffered and delivered in order. If a gap exists
    and max_delay_ms is exceeded, messages are force-flushed.
    """

    def __init__(
        self,
        window_size: int = 64,
        max_delay_ms: int = 5000,
    ):
        if window_size <= 0:
            raise ValueError("window_size must be positive")
        self._buffer: SortedDict[int, MessageEnvelope] = SortedDict()
        self._arrival_times: dict[int, float] = {}
        self._next_expected_seq: int = 0
        self._initialized: bool = False
        self._window_size = window_size
        self._max_delay_ms = max_delay_ms
        self._stats = ReorderBufferStats()

    def insert(self, msg: MessageEnvelope) -> list[MessageEnvelope]:
        """Insert message and return any deliverable messages in order.

        Returns ordered list of messages that can now be delivered.

        Note: The buffer starts ordering from the first message's sequence
        unless set_initial_sequence() is called. Messages arriving with
        a sequence lower than expected are dropped as "already delivered".
        For random-order use cases, call set_initial_sequence(min_seq) first.
        """
        seq = msg.sequence_no
        self._stats.messages_inserted += 1

        # Initialize next_expected on first message
        if not self._initialized:
            self._next_expected_seq = seq
            self._initialized = True

        # Ignore old messages (already delivered)
        if seq < self._next_expected_seq:
            return []

        # Already in buffer (duplicate seq)
        if seq in self._buffer:
            return []

        self._buffer[seq] = msg
        self._arrival_times[seq] = time.monotonic()

        if seq != self._next_expected_seq:
            self._stats.reorder_delays += 1

        return self._drain_consecutive()

    def flush(self) -> list[MessageEnvelope]:
        """Force-flush messages that have exceeded max_delay_ms.

        Messages waiting too long are delivered even with gaps.
        """
        if not self._buffer:
            return []

        now = time.monotonic()
        result: list[MessageEnvelope] = []

        # Check oldest message in buffer
        while self._buffer:
            oldest_seq = self._buffer.keys()[0]
            arrival = self._arrival_times.get(oldest_seq, now)
            elapsed_ms = (now - arrival) * 1000

            if elapsed_ms < self._max_delay_ms:
                break

            # Force deliver: skip gap
            self._next_expected_seq = oldest_seq
            delivered = self._drain_consecutive()
            result.extend(delivered)
            self._stats.forced_flushes += 1

            if not delivered:
                # Single stranded message
                msg = self._buffer.pop(oldest_seq)
                self._arrival_times.pop(oldest_seq, None)
                result.append(msg)
                self._next_expected_seq = oldest_seq + 1
                self._stats.messages_delivered += 1
                self._stats.forced_flushes += 1

        # Window overflow: force flush oldest
        while len(self._buffer) > self._window_size:
            oldest_seq = self._buffer.keys()[0]
            self._next_expected_seq = oldest_seq
            delivered = self._drain_consecutive()
            result.extend(delivered)
            if not delivered:
                msg = self._buffer.pop(oldest_seq)
                self._arrival_times.pop(oldest_seq, None)
                result.append(msg)
                self._next_expected_seq = oldest_seq + 1
                self._stats.messages_delivered += 1

        return result

    def set_initial_sequence(self, seq: int) -> list[MessageEnvelope]:
        """Set the expected sequence and return any messages now deliverable.

        Two use cases:
        1. Initialization (subscribe start, checkpoint resume) — buffer is empty,
           returns []
        2. Forward jump (after GapDetector reports UNRECOVERABLE) —
           advances next_expected_seq, drops buffer entries with seq < new next,
           then drains consecutive messages from the new position.

        Backward moves are forbidden (raises ValueError) to surface misuse.
        On the first call ever (uninitialized), any seq is accepted.
        """
        if self._initialized and seq < self._next_expected_seq:
            raise ValueError(
                f"set_initial_sequence cannot move backward: "
                f"current={self._next_expected_seq}, requested={seq}"
            )

        self._next_expected_seq = seq
        self._initialized = True

        # Drop entries that are now older than next_expected
        stale_keys = [k for k in self._buffer.keys() if k < seq]
        for k in stale_keys:
            self._buffer.pop(k, None)
            self._arrival_times.pop(k, None)

        return self._drain_consecutive()

    def _drain_consecutive(self) -> list[MessageEnvelope]:
        """Remove and return consecutive messages starting from next_expected_seq."""
        result: list[MessageEnvelope] = []
        while self._next_expected_seq in self._buffer:
            msg = self._buffer.pop(self._next_expected_seq)
            self._arrival_times.pop(self._next_expected_seq, None)
            result.append(msg)
            self._next_expected_seq += 1
            self._stats.messages_delivered += 1
        return result

    @property
    def pending_count(self) -> int:
        return len(self._buffer)

    @property
    def gaps(self) -> list[tuple[int, int]]:
        """Return detected gaps as [(start, end), ...]."""
        if not self._buffer or not self._initialized:
            return []

        result: list[tuple[int, int]] = []
        expected = self._next_expected_seq
        for seq in self._buffer.keys():
            if seq > expected:
                result.append((expected, seq - 1))
            expected = seq + 1
        return result

    @property
    def next_expected_sequence(self) -> int:
        return self._next_expected_seq

    @property
    def stats(self) -> ReorderBufferStats:
        self._stats.current_pending = len(self._buffer)
        return self._stats
