"""Abstract audio codec interface for tunnel data encoding."""

from __future__ import annotations

from abc import ABC, abstractmethod


class AudioCodec(ABC):
    """Base class for encoding/decoding tunnel data into audio packets."""

    @abstractmethod
    def encode(self, data: bytes) -> bytes:
        """Encode tunnel data into an Opus-compatible audio packet."""
        ...

    @abstractmethod
    def decode(self, opus_packet: bytes) -> bytes | None:
        """
        Decode tunnel data from an Opus audio packet.
        Returns None if the packet doesn't contain tunnel data (silence, real audio).
        """
        ...

    @abstractmethod
    def encode_silence(self) -> bytes:
        """Return a valid Opus packet representing silence / no data."""
        ...
