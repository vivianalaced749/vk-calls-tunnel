"""
Bot-side proxy engine.

Receives stream open requests from the multiplexer (SYN frames),
opens real TCP connections to the internet, and relays data
bidirectionally between the tunnel stream and the remote host.
"""

from __future__ import annotations

import asyncio

import structlog

from ..transport.mux import Stream, StreamMultiplexer, StreamState

log = structlog.get_logger()


class ProxyEngine:
    """
    Handles outbound TCP connections on the bot side.

    When a new tunnel stream opens (SYN), the proxy engine:
    1. Accepts the stream (sends SYN+ACK)
    2. Opens a real TCP connection to the target host:port
    3. Relays data bidirectionally until either side closes
    """

    def __init__(
        self,
        mux: StreamMultiplexer,
        connect_timeout: float = 30.0,
        idle_timeout: float = 60.0,
    ):
        self._mux = mux
        self._connect_timeout = connect_timeout
        self._idle_timeout = idle_timeout
        self._active_connections: dict[int, asyncio.Task] = {}

        # Wire up multiplexer callbacks
        self._mux.on_stream_open = self._on_stream_open
        self._mux.on_stream_close = self._on_stream_close

    async def _on_stream_open(self, stream: Stream) -> None:
        """Handle new stream from client (SYN received)."""
        log.info(
            "proxy: new stream",
            stream_id=stream.stream_id,
            host=stream.remote_host,
            port=stream.remote_port,
        )

        # Try to connect to the remote host
        try:
            remote_reader, remote_writer = await asyncio.wait_for(
                asyncio.open_connection(stream.remote_host, stream.remote_port),
                timeout=self._connect_timeout,
            )
        except (OSError, asyncio.TimeoutError) as e:
            log.warning(
                "proxy: connection failed",
                stream_id=stream.stream_id,
                host=stream.remote_host,
                port=stream.remote_port,
                error=str(e),
            )
            await self._mux.reject_stream(stream.stream_id)
            return

        # Accept the stream
        stream = await self._mux.accept_stream(
            stream.stream_id, stream.remote_host, stream.remote_port
        )

        # Start bidirectional relay
        task = asyncio.create_task(
            self._relay(stream, remote_reader, remote_writer)
        )
        self._active_connections[stream.stream_id] = task

    async def _on_stream_close(self, stream_id: int) -> None:
        """Handle stream close from client."""
        task = self._active_connections.pop(stream_id, None)
        if task and not task.done():
            task.cancel()

    async def _relay(
        self,
        stream: Stream,
        remote_reader: asyncio.StreamReader,
        remote_writer: asyncio.StreamWriter,
    ) -> None:
        """Bidirectional relay between tunnel stream and remote TCP connection."""
        try:
            await asyncio.gather(
                self._remote_to_tunnel(stream, remote_reader),
                self._tunnel_to_remote(stream, remote_writer),
            )
        except (asyncio.CancelledError, ConnectionError, OSError):
            pass
        finally:
            remote_writer.close()
            try:
                await remote_writer.wait_closed()
            except Exception:
                pass
            try:
                await self._mux.close_stream(stream.stream_id)
            except Exception:
                pass
            self._active_connections.pop(stream.stream_id, None)
            log.info("proxy: stream closed", stream_id=stream.stream_id)

    async def _remote_to_tunnel(
        self, stream: Stream, remote_reader: asyncio.StreamReader
    ) -> None:
        """Read from remote TCP, send through tunnel."""
        while True:
            data = await remote_reader.read(4096)
            if not data:
                break
            await self._mux.send(stream.stream_id, data)

    async def _tunnel_to_remote(
        self, stream: Stream, remote_writer: asyncio.StreamWriter
    ) -> None:
        """Read from tunnel, write to remote TCP."""
        while True:
            data = await self._mux.recv(stream.stream_id)
            if data is None:
                break
            remote_writer.write(data)
            await remote_writer.drain()

    async def stop(self) -> None:
        """Cancel all active connections."""
        for task in self._active_connections.values():
            task.cancel()
        await asyncio.gather(
            *self._active_connections.values(), return_exceptions=True
        )
        self._active_connections.clear()
