"""
Bot entry point — runs on a VPS outside Russia.

Starts:
1. Playwright browser with VK login
2. Waits for incoming VK call from the client
3. Transport protocol over the audio channel
4. Stream multiplexer
5. Proxy engine — opens real TCP connections to the internet
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
from .proxy_engine import ProxyEngine

log = structlog.get_logger()


class TunnelBot:
    """Full tunnel bot stack."""

    def __init__(self, config: TunnelConfig):
        self.config = config
        self.codec = DirectOpusCodec()
        self.fec = FECCodec(config.fec_parity_symbols)
        self.bridge: BrowserBridge | None = None
        self.transport: TransportProtocol | None = None
        self.mux: StreamMultiplexer | None = None
        self.proxy: ProxyEngine | None = None

    async def start(self) -> None:
        log.info("tunnel bot starting")

        # 1. Launch browser and login to VK
        self.bridge = BrowserBridge(self.config)
        await self.bridge.launch()

        # 2. Set up transport
        self.transport = TransportProtocol(
            send_callback=self._send_frame,
            window_size=self.config.window_size,
            retransmit_timeout_ms=self.config.retransmit_timeout_ms,
            max_retransmits=self.config.max_retransmits,
            keepalive_interval=self.config.keepalive_interval_s,
            link_dead_timeout=self.config.link_dead_timeout_s,
        )
        self.transport.on_link_dead = self._on_link_dead

        # 3. Set up multiplexer (bot role)
        self.mux = StreamMultiplexer(
            self.transport,
            role="bot",
            max_streams=self.config.max_streams,
            scheduler_quantum=self.config.stream_scheduler_quantum,
        )

        # 4. Set up proxy engine
        self.proxy = ProxyEngine(self.mux)

        # 5. Wire up audio
        self.bridge.on_audio_frame = self._on_incoming_audio
        self.bridge.on_link_ready = self._on_link_ready

        # 6. Wait for incoming call
        log.info("bot: waiting for incoming VK call...")
        await self.bridge.answer_call()

    async def _on_link_ready(self) -> None:
        """Called when WebRTC connection is established."""
        log.info("tunnel link ready — starting transport")
        await self.transport.start()
        await self.mux.start()
        log.info("bot: tunnel active — proxying to internet")

    async def _send_frame(self, frame: Frame) -> None:
        raw = frame.serialize()
        encoded = self.fec.encode(raw)
        opus_packet = self.codec.encode(encoded)
        await self.bridge.send_audio_frame(opus_packet)

    async def _on_incoming_audio(self, audio_data: bytes) -> None:
        decoded = self.codec.decode(audio_data)
        if decoded is None:
            return

        corrected = self.fec.decode(decoded)
        if corrected is None:
            log.warning("FEC decode failed — frame dropped")
            return

        frame = Frame.deserialize(corrected)
        if frame is None:
            return

        await self.transport.receive_frame(frame)

    async def _on_link_dead(self) -> None:
        log.warning("tunnel link dead — waiting for reconnect")
        await self.bridge.reconnect()

    async def stop(self) -> None:
        log.info("tunnel bot stopping")
        if self.proxy:
            await self.proxy.stop()
        if self.mux:
            await self.mux.stop()
        if self.transport:
            await self.transport.stop()
        if self.bridge:
            await self.bridge.close()


async def run_bot(config: TunnelConfig) -> None:
    """Run the tunnel bot."""
    bot = TunnelBot(config)
    try:
        await bot.start()
        await asyncio.Event().wait()
    except KeyboardInterrupt:
        pass
    finally:
        await bot.stop()
