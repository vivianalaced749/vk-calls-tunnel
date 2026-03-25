"""
Reliable transport protocol over the lossy audio channel.

Sliding window protocol with:
- Cumulative ACKs + selective ACKs (SACK)
- Timeout-based retransmission with exponential backoff
- Fast retransmit on 3 duplicate ACKs
- Simple AIMD congestion control
- Piggybacked ACKs on reverse data frames

The protocol operates on Frame objects and communicates through an
abstract send/receive interface (provided by the audio codec layer).
"""

from __future__ import annotations

import asyncio
import time
from collections import deque
from dataclasses import dataclass, field

import structlog

from .frame import Frame, FrameFlags, MAX_PAYLOAD, STREAM_CONTROL

log = structlog.get_logger()


@dataclass
class SentFrame:
    """Metadata for a frame in the send window."""
    frame: Frame
    sent_at: float
    retransmit_count: int = 0
    acked: bool = False


@dataclass
class TransportStats:
    """Live transport statistics."""
    frames_sent: int = 0
    frames_received: int = 0
    frames_retransmitted: int = 0
    frames_dropped: int = 0
    bytes_sent: int = 0
    bytes_received: int = 0
    rtt_ms: float = 250.0  # Estimated RTT (exponential moving average)


class TransportProtocol:
    """
    Reliable, ordered delivery over a lossy audio frame channel.

    Usage:
        tp = TransportProtocol(send_callback, config)
        await tp.start()

        # Send data (will be chunked into frames)
        await tp.send(stream_id, data)

        # Receive callback registered via on_receive
        tp.on_receive = my_callback

        # Feed incoming audio frames
        tp.receive_frame(frame)
    """

    def __init__(
        self,
        send_callback,  # async callable(Frame) -> None
        *,
        window_size: int = 32,
        retransmit_timeout_ms: int = 500,
        max_retransmits: int = 10,
        keepalive_interval: float = 5.0,
        link_dead_timeout: float = 15.0,
    ):
        self._send_raw = send_callback
        self._window_size = window_size
        self._rto_ms = retransmit_timeout_ms
        self._max_retransmits = max_retransmits
        self._keepalive_interval = keepalive_interval
        self._link_dead_timeout = link_dead_timeout

        # Sequence numbers
        self._next_seq: int = 0
        self._last_ack_sent: int = 0

        # Send window: seq → SentFrame
        self._send_window: dict[int, SentFrame] = {}
        self._send_queue: deque[Frame] = deque()

        # Receive buffer for reordering
        self._recv_buffer: dict[int, Frame] = {}
        self._next_expected_seq: int = 0

        # Congestion control
        self._cwnd: int = 4  # Congestion window
        self._ssthresh: int = window_size
        self._dup_ack_count: int = 0
        self._last_dup_ack_seq: int = -1

        # Timing
        self._last_received_at: float = time.monotonic()
        self._rtt_estimate: float = retransmit_timeout_ms / 1000.0

        # Callbacks
        self.on_receive = None   # async callable(stream_id: int, data: bytes)
        self.on_control = None   # async callable(Frame)  — SYN/FIN/RST frames
        self.on_link_dead = None # async callable()

        # Stats
        self.stats = TransportStats()

        # Tasks
        self._running = False
        self._tasks: list[asyncio.Task] = []
        self._send_event = asyncio.Event()

    async def start(self) -> None:
        self._running = True
        self._tasks = [
            asyncio.create_task(self._retransmit_loop()),
            asyncio.create_task(self._keepalive_loop()),
            asyncio.create_task(self._send_loop()),
        ]

    async def stop(self) -> None:
        self._running = False
        self._send_event.set()
        for task in self._tasks:
            task.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks.clear()

    def _alloc_seq(self) -> int:
        seq = self._next_seq
        self._next_seq = (self._next_seq + 1) & 0xFFFFFFFF
        return seq

    @property
    def effective_window(self) -> int:
        return min(self._cwnd, self._window_size)

    @property
    def in_flight(self) -> int:
        return sum(1 for sf in self._send_window.values() if not sf.acked)

    async def send(self, stream_id: int, data: bytes) -> None:
        """Queue data to be sent on a stream. Data is chunked into frames."""
        offset = 0
        while offset < len(data):
            chunk = data[offset : offset + MAX_PAYLOAD]
            frame = Frame.data(seq=0, stream_id=stream_id, payload=chunk)
            self._send_queue.append(frame)
            offset += len(chunk)
        self._send_event.set()

    async def send_control(self, frame: Frame) -> None:
        """Queue a control frame (SYN, FIN, RST)."""
        self._send_queue.append(frame)
        self._send_event.set()

    async def _send_loop(self) -> None:
        """Main send loop — dequeues frames and transmits within window."""
        while self._running:
            await self._send_event.wait()
            self._send_event.clear()

            while self._send_queue and self.in_flight < self.effective_window:
                frame = self._send_queue.popleft()
                frame.seq = self._alloc_seq()

                sent_frame = SentFrame(frame=frame, sent_at=time.monotonic())
                self._send_window[frame.seq] = sent_frame

                await self._send_raw(frame)
                self.stats.frames_sent += 1
                self.stats.bytes_sent += len(frame.payload)

                # Yield to let receive callbacks process
                await asyncio.sleep(0)

    async def receive_frame(self, frame: Frame) -> None:
        """Process an incoming frame from the audio channel."""
        self._last_received_at = time.monotonic()
        self.stats.frames_received += 1

        if frame.is_ack:
            self._process_ack(frame)
            return

        if frame.is_keepalive:
            return

        # Control frames (SYN, FIN, RST) — deliver to control handler
        if frame.is_syn or frame.is_fin or frame.is_rst:
            if self.on_control:
                await self.on_control(frame)
            self._advance_recv_seq(frame.seq)
            await self._send_ack()
            return

        # Data frame — buffer for ordered delivery
        if frame.seq >= self._next_expected_seq:
            self._recv_buffer[frame.seq] = frame
            self.stats.bytes_received += len(frame.payload)

        # Deliver in-order frames
        await self._deliver_ordered()

        # Send ACK
        await self._send_ack()

    def _advance_recv_seq(self, seq: int) -> None:
        if seq == self._next_expected_seq:
            self._next_expected_seq = (seq + 1) & 0xFFFFFFFF

    async def _deliver_ordered(self) -> None:
        """Deliver consecutive frames from the receive buffer."""
        while self._next_expected_seq in self._recv_buffer:
            frame = self._recv_buffer.pop(self._next_expected_seq)
            self._next_expected_seq = (self._next_expected_seq + 1) & 0xFFFFFFFF

            if self.on_receive and frame.payload:
                await self.on_receive(frame.stream_id, frame.payload)

    async def _send_ack(self) -> None:
        """Send cumulative ACK with optional SACK ranges."""
        ack_seq = (self._next_expected_seq - 1) & 0xFFFFFFFF

        # Build SACK ranges from out-of-order buffer
        sack_ranges = []
        if self._recv_buffer:
            sorted_seqs = sorted(self._recv_buffer.keys())
            range_start = sorted_seqs[0]
            range_end = sorted_seqs[0]
            for s in sorted_seqs[1:]:
                if s == (range_end + 1) & 0xFFFFFFFF:
                    range_end = s
                else:
                    sack_ranges.append((range_start, range_end))
                    range_start = range_end = s
            sack_ranges.append((range_start, range_end))

        # ACKs use seq=0 — they don't need ordered delivery and
        # shouldn't consume sequence numbers from the data stream
        ack_frame = Frame.ack(seq=0, ack_seq=ack_seq, sack_ranges=sack_ranges)
        await self._send_raw(ack_frame)
        self._last_ack_sent = ack_seq

    def _process_ack(self, frame: Frame) -> None:
        """Process incoming ACK — advance send window, update RTT."""
        ack_seq, sack_ranges = frame.parse_ack()
        now = time.monotonic()

        acked_any = False
        # Cumulative ACK: everything up to ack_seq is received
        seqs_to_remove = []
        for seq, sf in self._send_window.items():
            if seq <= ack_seq and not sf.acked:
                sf.acked = True
                acked_any = True
                seqs_to_remove.append(seq)

                # RTT update (only for non-retransmitted frames)
                if sf.retransmit_count == 0:
                    rtt = now - sf.sent_at
                    self._rtt_estimate = 0.875 * self._rtt_estimate + 0.125 * rtt
                    self.stats.rtt_ms = self._rtt_estimate * 1000

        # SACK: mark selectively acked frames
        for sack_start, sack_end in sack_ranges:
            for seq, sf in self._send_window.items():
                if sack_start <= seq <= sack_end and not sf.acked:
                    sf.acked = True
                    seqs_to_remove.append(seq)

        for seq in seqs_to_remove:
            self._send_window.pop(seq, None)

        # Congestion control
        if acked_any:
            self._dup_ack_count = 0
            if self._cwnd < self._ssthresh:
                self._cwnd += 1  # Slow start
            else:
                self._cwnd += 1.0 / self._cwnd  # Congestion avoidance

            # Wake send loop — window may have opened
            self._send_event.set()
        else:
            # Duplicate ACK
            if ack_seq == self._last_dup_ack_seq:
                self._dup_ack_count += 1
            else:
                self._dup_ack_count = 1
                self._last_dup_ack_seq = ack_seq

            # Fast retransmit
            if self._dup_ack_count >= 3:
                self._ssthresh = max(self._cwnd // 2, 2)
                self._cwnd = self._ssthresh
                target_seq = (ack_seq + 1) & 0xFFFFFFFF
                if target_seq in self._send_window:
                    sf = self._send_window[target_seq]
                    asyncio.create_task(self._retransmit(sf))

    async def _retransmit(self, sf: SentFrame) -> None:
        """Retransmit a single frame."""
        if sf.retransmit_count >= self._max_retransmits:
            log.warning("max retransmits exceeded", seq=sf.frame.seq)
            self.stats.frames_dropped += 1
            return

        sf.retransmit_count += 1
        sf.sent_at = time.monotonic()
        await self._send_raw(sf.frame)
        self.stats.frames_retransmitted += 1

    async def _retransmit_loop(self) -> None:
        """Periodic check for timed-out frames."""
        while self._running:
            await asyncio.sleep(0.05)  # Check every 50ms
            now = time.monotonic()
            rto = max(self._rtt_estimate * 2, self._rto_ms / 1000.0)

            for sf in list(self._send_window.values()):
                if not sf.acked and (now - sf.sent_at) > rto:
                    # Timeout — halve congestion window
                    self._ssthresh = max(self._cwnd // 2, 2)
                    self._cwnd = 1
                    await self._retransmit(sf)

    async def _keepalive_loop(self) -> None:
        """Send keepalives and detect dead links."""
        while self._running:
            await asyncio.sleep(self._keepalive_interval)
            now = time.monotonic()

            # Link dead detection
            if (now - self._last_received_at) > self._link_dead_timeout:
                log.error("link dead — no frames received", timeout=self._link_dead_timeout)
                if self.on_link_dead:
                    await self.on_link_dead()
                continue

            # Send keepalive if idle
            if not self._send_window and not self._send_queue:
                kal = Frame.keepalive(seq=0)
                await self._send_raw(kal)
