"""Live tunnel statistics."""

from __future__ import annotations

import time
from dataclasses import dataclass, field


@dataclass
class TunnelStats:
    """Aggregated tunnel statistics."""
    started_at: float = field(default_factory=time.monotonic)

    # Throughput
    bytes_sent: int = 0
    bytes_received: int = 0

    # Frames
    frames_sent: int = 0
    frames_received: int = 0
    frames_retransmitted: int = 0
    frames_dropped: int = 0

    # Connection
    reconnects: int = 0
    active_streams: int = 0
    rtt_ms: float = 0.0

    @property
    def uptime_s(self) -> float:
        return time.monotonic() - self.started_at

    @property
    def send_rate_kbps(self) -> float:
        elapsed = self.uptime_s
        if elapsed <= 0:
            return 0.0
        return (self.bytes_sent * 8) / (elapsed * 1000)

    @property
    def recv_rate_kbps(self) -> float:
        elapsed = self.uptime_s
        if elapsed <= 0:
            return 0.0
        return (self.bytes_received * 8) / (elapsed * 1000)

    def summary(self) -> str:
        return (
            f"Uptime: {self.uptime_s:.0f}s | "
            f"TX: {self.send_rate_kbps:.1f} kbps | "
            f"RX: {self.recv_rate_kbps:.1f} kbps | "
            f"RTT: {self.rtt_ms:.0f}ms | "
            f"Streams: {self.active_streams} | "
            f"Retransmits: {self.frames_retransmitted} | "
            f"Drops: {self.frames_dropped}"
        )
