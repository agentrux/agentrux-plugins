"""Sequence gap detector for pipeline.

SSOT: docs/04_design/sdk/sdk_design.md §6
"""

from __future__ import annotations

from agentrux_sdk.errors import GapDetectedError


class SequenceGapDetector:
    """連続 sequence_number で gap を検出。 検出時は GapDetectedError を raise.

    window: 「想定される最大遅延 event 数」。 sequence_number が `_last + 1` でなくても、
    `_last + 1 .. _last + window` の範囲内なら一時的な順序ずれと見なす (reorder buffer が解決)。
    window を超える gap は欠落として GapDetectedError。
    """

    def __init__(self, *, window: int = 1000) -> None:
        self.window = window
        self._last_seen: int | None = None

    def observe(self, sequence_number: int, *, topic_id: str) -> None:
        if self._last_seen is None:
            self._last_seen = sequence_number
            return
        if sequence_number <= self._last_seen:
            # 重複 / 古い event は無視 (at-least-once 想定)
            return
        gap = sequence_number - self._last_seen - 1
        if gap > self.window:
            raise GapDetectedError(
                f"sequence gap detected after {self._last_seen}: next observed {sequence_number} (gap {gap})",
                topic_id=topic_id,
                gap_after=self._last_seen,
                gap_size=gap,
            )
        self._last_seen = sequence_number

    @property
    def last_seen(self) -> int | None:
        return self._last_seen
