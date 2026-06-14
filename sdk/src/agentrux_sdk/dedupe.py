"""at-least-once 重複排除 — event_id の bounded window set.

SSOT: docs/04_design/sdk/sdk_design.md §5-1 (重複は client が event_id で dedupe),
      docs/04_design/messaging/cluster_agnostic_ordering.md §2 (重複は client が dedupe)

旧 gap_detector / reorder_buffer は seq 連番依存のため撤去。
新契約では順序保証なし・at-least-once 保証のみ。SDK は event_id の直近 N 件
bounded set で重複を排除し、それ以外の責務は持たない。
"""

from __future__ import annotations


class EventIdDedupe:
    """直近 N 件の event_id を bounded set で管理し、重複受信を排除する.

    at-least-once 配信では同一 event が複数回 deliver される可能性がある。
    本クラスは window サイズを固定して O(1) amortized で重複チェックを行う。

    Args:
        window: 記憶する event_id の上限件数。 古いものから順に忘れる。
                デフォルト 10_000 は pipeline 典型ワークロード向け。
    """

    def __init__(self, *, window: int = 10_000) -> None:
        if window < 1:
            raise ValueError(f"window must be >= 1 (got {window})")
        self._window = window
        self._seen: set[str] = set()
        self._order: list[str] = []  # insertion order for eviction

    def is_duplicate(self, event_id: str) -> bool:
        """True なら重複 (既に処理済)、 False なら初回 (seen に追加)."""
        if event_id in self._seen:
            return True
        # 上限超過なら最古のエントリを evict
        if len(self._seen) >= self._window:
            oldest = self._order.pop(0)
            self._seen.discard(oldest)
        self._seen.add(event_id)
        self._order.append(event_id)
        return False

    @property
    def seen_count(self) -> int:
        """現在記憶中の event_id 件数 (window 以下)."""
        return len(self._seen)
