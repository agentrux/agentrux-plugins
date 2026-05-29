"""Reorder buffer for pipeline (順序ずれの一時保留).

SSOT: docs/04_design/sdk/sdk_design.md §6

invariant:
- sequence_number 昇順で出力
- buffer 内にある event は max_lag 個を超えない
- push() は「今出力できる連続 prefix」を返す
"""

from __future__ import annotations

from typing import Any


class ReorderBuffer:
    """bounded buffer。 sequence_number で sort + 連続 prefix の flush.

    呼び出し例:
      buf = ReorderBuffer(max_lag=100)
      out = await buf.push(sequence_number=5, event=evt)  # [evt(3), evt(4), evt(5)] (依存先 prefix が来た時)
    """

    def __init__(self, *, max_lag: int = 100) -> None:
        self.max_lag = max_lag
        self._buffer: dict[int, Any] = {}
        self._next_expected: int | None = None  # 次に出力すべき sequence_number

    async def push(self, *, sequence_number: int, event: Any) -> list[Any]:
        """event を buffer に追加し、 出力可能な連続 prefix を返す.

        - 初回 push: _next_expected を sequence_number に初期化、 そのまま 1 件出力
        - 重複 (既に出力済 / buffer 済): 無視して []
        - 期待外 future seq: buffer に積む、 max_lag 超過は buffer の最古を強制 flush (gap 容認)
        """
        if self._next_expected is None:
            self._next_expected = sequence_number

        if sequence_number < self._next_expected:
            # 古い event (重複) → 無視
            return []
        if sequence_number in self._buffer:
            # 重複 → 無視
            return []

        self._buffer[sequence_number] = event

        # max_lag 超過: 待っている prefix を諦め、 buffer 内最小 seq を新しい next_expected に
        if len(self._buffer) > self.max_lag:
            self._next_expected = min(self._buffer.keys())

        # 連続 prefix を flush
        flushed: list[Any] = []
        while self._next_expected in self._buffer:
            flushed.append(self._buffer.pop(self._next_expected))
            self._next_expected += 1
        return flushed

    @property
    def pending_count(self) -> int:
        return len(self._buffer)
