"""
Stream multiplexer — maps multiple concurrent TCP connections onto
a single bidirectional audio channel.

Each SOCKS5 connection gets a unique 16-bit stream ID.
The multiplexer handles:
- Stream open/close lifecycle (SYN/FIN/RST)
- Round-robin scheduling across active streams
- Flow control per-stream
- Buffering incoming data per-stream

Stream IDs:
  0x0000 = control channel
  0x0001-0xFFFE = data streams
  0xFFFF = broadcast
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from enum import Enum

import structlog

from .frame import Frame, FrameFlags, MAX_PAYLOAD
from .protocol import TransportProtocol

log = structlog.get_logger()


class StreamState(Enum):
    OPENING = "opening"
    OPEN = "open"
    CLOSING = "closing"
    CLOSED = "closed"


@dataclass
class Stream:
    """A single multiplexed stream (maps to one TCP connection)."""
    stream_id: int
    state: StreamState = StreamState.OPENING
    remote_host: str = ""
    remote_port: int = 0

    # Incoming data buffer (from remote, to be read by SOCKS5 handler)
    recv_queue: asyncio.Queue[bytes | None] = field(default_factory=lambda: asyncio.Queue())

    # Outgoing data buffer (from SOCKS5 handler, to be sent over tunnel)
    send_buffer: bytearray = field(default_factory=bytearray)

    # Events
    open_event: asyncio.Event = field(default_factory=asyncio.Event)
    close_event: asyncio.Event = field(default_factory=asyncio.Event)


class StreamMultiplexer:
    """
    Multiplexes multiple TCP streams over a single TransportProtocol.

    Client side:
        mux = StreamMultiplexer(transport, role="client")
        stream = await mux.open_stream("example.com", 443)
        await mux.send(stream.stream_id, data)
        data = await mux.recv(stream.stream_id)

    Bot side:
        mux = StreamMultiplexer(transport, role="bot")
        mux.on_stream_open = handle_new_stream
    """

    def __init__(
        self,
        transport: TransportProtocol,
        role: str = "client",  # "client" or "bot"
        max_streams: int = 256,
        scheduler_quantum: int = 4,
    ):
        self._transport = transport
        self._role = role
        self._max_streams = max_streams
        self._quantum = scheduler_quantum

        self._streams: dict[int, Stream] = {}
        self._next_stream_id = 1  # Client allocates odd IDs

        # Callbacks
        self.on_stream_open = None   # async callable(Stream) — bot side, new stream request
        self.on_stream_close = None  # async callable(int)  — stream_id closed by remote

        # Wire up transport callbacks
        self._transport.on_receive = self._on_data
        self._transport.on_control = self._on_control

        self._scheduler_task: asyncio.Task | None = None

    async def start(self) -> None:
        self._scheduler_task = asyncio.create_task(self._scheduler_loop())

    async def stop(self) -> None:
        if self._scheduler_task:
            self._scheduler_task.cancel()
            try:
                await self._scheduler_task
            except asyncio.CancelledError:
                pass

        # Close all streams
        for stream in list(self._streams.values()):
            stream.state = StreamState.CLOSED
            stream.recv_queue.put_nowait(None)
            stream.close_event.set()

    async def open_stream(self, host: str, port: int, timeout: float = 30.0) -> Stream:
        """Open a new stream (client side). Sends SYN, waits for SYN+ACK."""
        stream_id = self._alloc_stream_id()
        stream = Stream(
            stream_id=stream_id,
            state=StreamState.OPENING,
            remote_host=host,
            remote_port=port,
        )
        self._streams[stream_id] = stream

        # Send SYN
        syn = Frame.syn(seq=0, stream_id=stream_id, host=host, port=port)
        await self._transport.send_control(syn)

        # Wait for SYN+ACK (open_event set by _on_control)
        try:
            await asyncio.wait_for(stream.open_event.wait(), timeout=timeout)
        except asyncio.TimeoutError:
            self._streams.pop(stream_id, None)
            raise ConnectionError(f"Stream open timeout: {host}:{port}")

        if stream.state == StreamState.CLOSED:
            self._streams.pop(stream_id, None)
            raise ConnectionRefusedError(f"Stream refused: {host}:{port}")

        return stream

    async def accept_stream(self, stream_id: int, host: str, port: int) -> Stream:
        """Accept a stream (bot side). Sends SYN+ACK back."""
        stream = self._streams.get(stream_id)
        if not stream:
            stream = Stream(
                stream_id=stream_id,
                remote_host=host,
                remote_port=port,
                state=StreamState.OPEN,
            )
            self._streams[stream_id] = stream

        stream.state = StreamState.OPEN
        stream.open_event.set()

        # Send SYN+ACK
        syn_ack = Frame(
            flags=FrameFlags.SYN | FrameFlags.ACK,
            seq=0,
            stream_id=stream_id,
        )
        await self._transport.send_control(syn_ack)
        return stream

    async def reject_stream(self, stream_id: int) -> None:
        """Reject a stream open request (bot side). Sends RST."""
        rst = Frame.rst(seq=0, stream_id=stream_id)
        await self._transport.send_control(rst)
        self._cleanup_stream(stream_id)

    async def send(self, stream_id: int, data: bytes) -> None:
        """Queue data to send on a stream."""
        stream = self._streams.get(stream_id)
        if not stream or stream.state != StreamState.OPEN:
            raise ConnectionError(f"Stream {stream_id} not open")
        stream.send_buffer.extend(data)

    async def recv(self, stream_id: int) -> bytes | None:
        """
        Read data from a stream. Blocks until data is available.
        Returns None when the stream is closed.
        """
        stream = self._streams.get(stream_id)
        if not stream:
            return None
        return await stream.recv_queue.get()

    async def close_stream(self, stream_id: int) -> None:
        """Gracefully close a stream. Sends FIN."""
        stream = self._streams.get(stream_id)
        if not stream:
            return
        stream.state = StreamState.CLOSING
        fin = Frame.fin(seq=0, stream_id=stream_id)
        await self._transport.send_control(fin)

        # Wait for FIN+ACK or timeout
        try:
            await asyncio.wait_for(stream.close_event.wait(), timeout=5.0)
        except asyncio.TimeoutError:
            pass
        self._cleanup_stream(stream_id)

    def _alloc_stream_id(self) -> int:
        while True:
            sid = self._next_stream_id
            self._next_stream_id += 1
            if self._next_stream_id > 0xFFFE:
                self._next_stream_id = 1
            if sid not in self._streams:
                return sid

    def _cleanup_stream(self, stream_id: int) -> None:
        stream = self._streams.pop(stream_id, None)
        if stream:
            stream.state = StreamState.CLOSED
            stream.recv_queue.put_nowait(None)
            stream.close_event.set()

    async def _on_data(self, stream_id: int, data: bytes) -> None:
        """Handle incoming data frame from transport."""
        stream = self._streams.get(stream_id)
        if stream and stream.state == StreamState.OPEN:
            await stream.recv_queue.put(data)

    async def _on_control(self, frame: Frame) -> None:
        """Handle incoming control frame (SYN, SYN+ACK, FIN, RST)."""
        stream_id = frame.stream_id

        if frame.is_syn and frame.is_ack:
            # SYN+ACK — stream opened by remote (response to our SYN)
            stream = self._streams.get(stream_id)
            if stream:
                stream.state = StreamState.OPEN
                stream.open_event.set()

        elif frame.is_syn:
            # SYN — remote wants to open a stream (bot side)
            host, port = frame.parse_syn()
            stream = Stream(
                stream_id=stream_id,
                state=StreamState.OPENING,
                remote_host=host,
                remote_port=port,
            )
            self._streams[stream_id] = stream

            if self.on_stream_open:
                await self.on_stream_open(stream)

        elif frame.is_fin and frame.is_ack:
            # FIN+ACK — remote acknowledges our FIN
            stream = self._streams.get(stream_id)
            if stream:
                stream.close_event.set()
                self._cleanup_stream(stream_id)

        elif frame.is_fin:
            # FIN — remote wants to close
            stream = self._streams.get(stream_id)
            if stream:
                stream.recv_queue.put_nowait(None)
                # Send FIN+ACK
                fin_ack = Frame(
                    flags=FrameFlags.FIN | FrameFlags.ACK,
                    seq=0,
                    stream_id=stream_id,
                )
                await self._transport.send_control(fin_ack)
                self._cleanup_stream(stream_id)

                if self.on_stream_close:
                    await self.on_stream_close(stream_id)

        elif frame.is_rst:
            # RST — remote force-closed
            stream = self._streams.get(stream_id)
            if stream:
                stream.state = StreamState.CLOSED
                stream.open_event.set()  # Unblock open_stream waiters
                self._cleanup_stream(stream_id)

    async def _scheduler_loop(self) -> None:
        """Round-robin send scheduler across active streams."""
        while True:
            sent_any = False
            for stream in list(self._streams.values()):
                if stream.state != StreamState.OPEN:
                    continue
                if not stream.send_buffer:
                    continue

                # Send up to quantum chunks from this stream
                for _ in range(self._quantum):
                    if not stream.send_buffer:
                        break
                    chunk = bytes(stream.send_buffer[:MAX_PAYLOAD])
                    del stream.send_buffer[:len(chunk)]
                    await self._transport.send(stream.stream_id, chunk)
                    sent_any = True

            if not sent_any:
                await asyncio.sleep(0.01)  # Yield when idle
