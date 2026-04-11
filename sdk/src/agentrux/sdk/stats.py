"""SDK statistics dataclasses."""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class DeduplicatorStats:
    total_seen: int = 0
    duplicates_dropped: int = 0
    current_size: int = 0


@dataclass
class ReorderBufferStats:
    messages_inserted: int = 0
    messages_delivered: int = 0
    reorder_delays: int = 0
    forced_flushes: int = 0
    current_pending: int = 0


@dataclass
class FlowControllerStats:
    messages_pushed: int = 0
    messages_pulled: int = 0
    pause_count: int = 0
    current_buffer_size: int = 0
    buffer_utilization: float = 0.0


@dataclass
class GapDetectorStats:
    gaps_detected: int = 0
    gaps_filled: int = 0
    gaps_unrecoverable: int = 0
    backfill_requests: int = 0


@dataclass
class PipelineStats:
    deduplicator: DeduplicatorStats = field(default_factory=DeduplicatorStats)
    reorder_buffer: ReorderBufferStats = field(default_factory=ReorderBufferStats)
    flow_controller: FlowControllerStats = field(default_factory=FlowControllerStats)
    gap_detector: GapDetectorStats | None = None


@dataclass
class SDKStats:
    messages_received: int = 0
    messages_delivered: int = 0
    duplicates_dropped: int = 0
    reorder_delays: int = 0
    reconnections: int = 0
    errors: int = 0
    current_mode: str = "disconnected"
    buffer_utilization: float = 0.0
    avg_delivery_latency_ms: float = 0.0
