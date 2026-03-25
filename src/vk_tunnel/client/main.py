"""
Client entry point — runs on the machine inside Russia.

Starts:
1. Playwright browser with VK login
2. Initiates VK call to the bot
3. Transport protocol over the audio channel
4. Stream multiplexer
5. SOCKS5 proxy on localhost

Applications connect to the SOCKS5 proxy → data flows through VK call → internet.
"""

from __future__ import annotations

import asyncio

import structlog

from ..config import TunnelConfig
from ..codec.direct_opus import DirectOpusCodec
from ..codec.fec import FECCodec
from ..transport.frame import Frame
from ..transport.protocol import TransportProtocol
from ..transport.mux import StreamMultiplexer
from ..vk.browser_bridge import BrowserBridge
from .socks5_server import Socks5Server

log = structlog.get_logger()


class TunnelClient:
    """Full tunnel client stack."""

    def __init__(self, config: TunnelConfig):
        self.config = config
        self.codec = DirectOpusCodec()
        self.fec = FECCodec(config.fec_parity_symbols)
        self.bridge: BrowserBridge | None = None
        self.transport: TransportProtocol | None = None
        self.mux: StreamMultiplexer | None = None
        self.socks5: Socks5Server | None = None

    async def start(self) -> None:
        log.info("tunnel client starting")

        # 1. Launch browser and login to VK
        self.bridge = BrowserBridge(self.config)
        await self.bridge.launch()

        # 2. Set up transport protocol
        self.transport = TransportProtocol(
            send_callback=self._send_frame,
            window_size=self.config.window_size,
            retransmit_timeout_ms=self.config.retransmit_timeout_ms,
            max_retransmits=self.config.max_retransmits,
            keepalive_interval=self.config.keepalive_interval_s,
            link_dead_timeout=self.config.link_dead_timeout_s,
        )
        self.transport.on_link_dead = self._on_link_dead

        # 3. Set up multiplexer
        self.mux = StreamMultiplexer(
            self.transport,
            role="client",
            max_streams=self.config.max_streams,
            scheduler_quantum=self.config.stream_scheduler_quantum,
        )

        # 4. Set up SOCKS5 server
        self.socks5 = Socks5Server(
            self.mux,
            host=self.config.socks5_host,
            port=self.config.socks5_port,
        )

        # 5. Wire up incoming audio → transport
        self.bridge.on_audio_frame = self._on_incoming_audio
        self.bridge.on_link_ready = self._on_link_ready

        # 6. Initiate VK call
        await self.bridge.start_call()

    async def _on_link_ready(self) -> None:
        """Called when WebRTC connection is established."""
        log.info("tunnel link ready — starting transport")
        await self.transport.start()
        await self.mux.start()
        await self.socks5.start()
        log.info(
            "tunnel active",
            socks5=f"{self.config.socks5_host}:{self.config.socks5_port}",
        )

    async def _send_frame(self, frame: Frame) -> None:
        """Serialize frame → FEC encode → Opus encode → send via browser."""
        raw = frame.serialize()
        encoded = self.fec.encode(raw)
        opus_packet = self.codec.encode(encoded)
        await self.bridge.send_audio_frame(opus_packet)

    async def _on_incoming_audio(self, audio_data: bytes) -> None:
        """Incoming audio → Opus decode → FEC decode → deserialize → transport."""
        decoded = self.codec.decode(audio_data)
        if decoded is None:
            return  # Silence or non-tunnel audio

        corrected = self.fec.decode(decoded)
        if corrected is None:
            log.warning("FEC decode failed — frame dropped")
            return

        frame = Frame.deserialize(corrected)
        if frame is None:
            return  # Invalid frame (bad magic or CRC)

        await self.transport.receive_frame(frame)

    async def _on_link_dead(self) -> None:
        """Handle dead link — stop proxy, attempt reconnect."""
        log.warning("tunnel link dead — reconnecting")
        if self.socks5:
            await self.socks5.stop()
        await self.bridge.reconnect()

    async def stop(self) -> None:
        """Graceful shutdown."""
        log.info("tunnel client stopping")
        if self.socks5:
            await self.socks5.stop()
        if self.mux:
            await self.mux.stop()
        if self.transport:
            await self.transport.stop()
        if self.bridge:
            await self.bridge.close()


async def run_client(config: TunnelConfig) -> None:
    """Run the tunnel client."""
    client = TunnelClient(config)
    try:
        await client.start()
        # Keep running until interrupted
        await asyncio.Event().wait()
    except KeyboardInterrupt:
        pass
    finally:
        await client.stop()
