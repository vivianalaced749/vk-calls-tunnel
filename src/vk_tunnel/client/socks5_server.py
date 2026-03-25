"""
SOCKS5 proxy server (RFC 1928).

Listens on localhost, accepts CONNECT commands from local applications,
and tunnels them through the VK call audio channel via the multiplexer.

Supports:
- No authentication method (0x00)
- CONNECT command only (0x01)
- IPv4 and domain name address types
"""

from __future__ import annotations

import asyncio
import struct

import structlog

from ..transport.mux import StreamMultiplexer

log = structlog.get_logger()

# SOCKS5 constants
VER = 0x05
AUTH_NONE = 0x00
CMD_CONNECT = 0x01
ATYP_IPV4 = 0x01
ATYP_DOMAIN = 0x03
ATYP_IPV6 = 0x04
REP_SUCCESS = 0x00
REP_GENERAL_FAILURE = 0x01
REP_CONN_REFUSED = 0x05
REP_HOST_UNREACHABLE = 0x04
RSV = 0x00


class Socks5Server:
    """
    Local SOCKS5 proxy that tunnels connections through the VK call.

    Usage:
        server = Socks5Server(mux, host="127.0.0.1", port=1080)
        await server.start()
        # ... server is running, accepting connections ...
        await server.stop()
    """

    def __init__(
        self,
        mux: StreamMultiplexer,
        host: str = "127.0.0.1",
        port: int = 1080,
    ):
        self._mux = mux
        self._host = host
        self._port = port
        self._server: asyncio.Server | None = None

    async def start(self) -> None:
        self._server = await asyncio.start_server(
            self._handle_client, self._host, self._port
        )
        log.info("socks5 server started", host=self._host, port=self._port)

    async def stop(self) -> None:
        if self._server:
            self._server.close()
            await self._server.wait_closed()
            log.info("socks5 server stopped")

    async def _handle_client(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        """Handle a single SOCKS5 client connection."""
        addr = writer.get_extra_info("peername")
        stream_id = None

        try:
            # --- Greeting ---
            # Client sends: VER | NMETHODS | METHODS
            header = await reader.readexactly(2)
            ver, nmethods = header
            if ver != VER:
                writer.close()
                return
            methods = await reader.readexactly(nmethods)

            # We only support no-auth
            if AUTH_NONE not in methods:
                writer.write(bytes([VER, 0xFF]))  # No acceptable methods
                await writer.drain()
                writer.close()
                return

            # Select no-auth
            writer.write(bytes([VER, AUTH_NONE]))
            await writer.drain()

            # --- Request ---
            # Client sends: VER | CMD | RSV | ATYP | DST.ADDR | DST.PORT
            req_header = await reader.readexactly(4)
            ver, cmd, _, atyp = req_header

            if cmd != CMD_CONNECT:
                await self._send_reply(writer, REP_GENERAL_FAILURE)
                writer.close()
                return

            # Parse destination address
            if atyp == ATYP_IPV4:
                addr_bytes = await reader.readexactly(4)
                host = ".".join(str(b) for b in addr_bytes)
            elif atyp == ATYP_DOMAIN:
                domain_len = (await reader.readexactly(1))[0]
                host = (await reader.readexactly(domain_len)).decode("utf-8")
            elif atyp == ATYP_IPV6:
                addr_bytes = await reader.readexactly(16)
                # Format as IPv6 string
                parts = struct.unpack("!8H", addr_bytes)
                host = ":".join(f"{p:x}" for p in parts)
            else:
                await self._send_reply(writer, REP_GENERAL_FAILURE)
                writer.close()
                return

            port_bytes = await reader.readexactly(2)
            port = struct.unpack("!H", port_bytes)[0]

            log.info("socks5 connect", host=host, port=port, client=addr)

            # --- Open tunnel stream ---
            try:
                stream = await self._mux.open_stream(host, port)
                stream_id = stream.stream_id
            except ConnectionRefusedError:
                await self._send_reply(writer, REP_CONN_REFUSED)
                writer.close()
                return
            except (ConnectionError, asyncio.TimeoutError):
                await self._send_reply(writer, REP_HOST_UNREACHABLE)
                writer.close()
                return

            # Success reply
            await self._send_reply(writer, REP_SUCCESS)

            # --- Relay data bidirectionally ---
            await asyncio.gather(
                self._relay_client_to_tunnel(reader, stream_id),
                self._relay_tunnel_to_client(writer, stream_id),
            )

        except (asyncio.IncompleteReadError, ConnectionResetError, OSError):
            pass
        finally:
            if stream_id is not None:
                try:
                    await self._mux.close_stream(stream_id)
                except Exception:
                    pass
            writer.close()
            try:
                await writer.wait_closed()
            except Exception:
                pass

    async def _send_reply(self, writer: asyncio.StreamWriter, rep: int) -> None:
        """Send SOCKS5 reply: VER | REP | RSV | ATYP | BND.ADDR | BND.PORT"""
        reply = bytes([VER, rep, RSV, ATYP_IPV4, 0, 0, 0, 0, 0, 0])
        writer.write(reply)
        await writer.drain()

    async def _relay_client_to_tunnel(
        self, reader: asyncio.StreamReader, stream_id: int
    ) -> None:
        """Read from local TCP client, send through tunnel."""
        try:
            while True:
                data = await reader.read(4096)
                if not data:
                    break
                await self._mux.send(stream_id, data)
        except (ConnectionResetError, OSError):
            pass

    async def _relay_tunnel_to_client(
        self, writer: asyncio.StreamWriter, stream_id: int
    ) -> None:
        """Read from tunnel stream, write to local TCP client."""
        try:
            while True:
                data = await self._mux.recv(stream_id)
                if data is None:
                    break
                writer.write(data)
                await writer.drain()
        except (ConnectionResetError, OSError):
            pass
