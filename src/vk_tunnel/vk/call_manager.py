"""
VK call lifecycle manager.

Manages the state machine:
  IDLE → SIGNALING → PROBING → CONNECTED → RECONNECT → SIGNALING (loop)

Handles call initiation (client), call answering (bot),
auto-reconnection on drops, and call rotation for anti-detection.
"""

from __future__ import annotations

import asyncio
import random
import time
from enum import Enum

import structlog

from ..config import TunnelConfig

log = structlog.get_logger()


class CallState(Enum):
    IDLE = "idle"
    SIGNALING = "signaling"
    PROBING = "probing"
    CONNECTED = "connected"
    RECONNECTING = "reconnecting"
    DEAD = "dead"


class CallManager:
    """
    Abstract call lifecycle manager.
    Concrete implementations (BrowserBridge or NativeWebRTC) must implement
    the actual call signaling and media handling.
    """

    def __init__(self, config: TunnelConfig):
        self._config = config
        self._state = CallState.IDLE
        self._reconnect_attempts = 0
        self._call_started_at: float | None = None
        self._session_token: bytes | None = None

        # Callbacks
        self.on_audio_frame = None    # async callable(bytes) — incoming Opus frame
        self.on_state_change = None   # async callable(CallState)
        self.on_link_ready = None     # async callable() — tunnel is ready

        self._rotation_task: asyncio.Task | None = None

    @property
    def state(self) -> CallState:
        return self._state

    async def _set_state(self, state: CallState) -> None:
        old = self._state
        self._state = state
        log.info("call state", old=old.value, new=state.value)
        if self.on_state_change:
            await self.on_state_change(state)

    async def start_call(self) -> None:
        """Initiate or answer a call (implemented by subclass)."""
        raise NotImplementedError

    async def end_call(self) -> None:
        """Hang up the current call (implemented by subclass)."""
        raise NotImplementedError

    async def send_audio_frame(self, opus_packet: bytes) -> None:
        """Send an Opus audio frame through the call (implemented by subclass)."""
        raise NotImplementedError

    async def reconnect(self) -> None:
        """Attempt to reconnect after call drop."""
        await self._set_state(CallState.RECONNECTING)

        while self._reconnect_attempts < self._config.max_reconnect_attempts:
            self._reconnect_attempts += 1
            backoff = min(
                self._config.reconnect_backoff_base_s * (2 ** (self._reconnect_attempts - 1)),
                self._config.reconnect_backoff_max_s,
            )
            # Add jitter (±20%)
            backoff *= random.uniform(0.8, 1.2)
            log.info(
                "reconnecting",
                attempt=self._reconnect_attempts,
                backoff_s=f"{backoff:.1f}",
            )
            await asyncio.sleep(backoff)

            try:
                await self.start_call()
                self._reconnect_attempts = 0
                return
            except Exception as e:
                log.warning("reconnect failed", error=str(e))

        log.error("max reconnect attempts reached")
        await self._set_state(CallState.DEAD)

    async def _start_rotation_timer(self) -> None:
        """Rotate call (hang up + re-call) at random intervals for anti-detection."""
        if self._rotation_task:
            self._rotation_task.cancel()

        async def _rotate():
            while True:
                interval = random.randint(
                    self._config.call_rotation_min_m * 60,
                    self._config.call_rotation_max_m * 60,
                )
                await asyncio.sleep(interval)
                log.info("call rotation — reconnecting for anti-detection")
                await self.end_call()
                await asyncio.sleep(2)
                await self.start_call()

        self._rotation_task = asyncio.create_task(_rotate())

    async def stop(self) -> None:
        """Clean shutdown."""
        if self._rotation_task:
            self._rotation_task.cancel()
            try:
                await self._rotation_task
            except asyncio.CancelledError:
                pass
        try:
            await self.end_call()
        except Exception:
            pass
        await self._set_state(CallState.IDLE)
