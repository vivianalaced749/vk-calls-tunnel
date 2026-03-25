"""
Mode A: Direct Opus payload replacement.

Constructs valid Opus packet headers but replaces the compressed audio
payload with tunnel data. This gives maximum throughput (~70 kbps)
but won't survive if VK media servers transcode the audio.

Opus TOC byte format (RFC 6716):
  - config (5 bits): codec mode + bandwidth + frame size
  - s (1 bit): stereo flag
  - c (2 bits): frame count code

We use config=16 (CELT-only, narrowband, 20ms), mono, code=0 (single frame).
TOC = 0x80.

Packet format:
  [TOC byte = 0x80] [length byte] [data bytes] [padding to min size]

The length byte tells the decoder how many actual data bytes follow,
so we can distinguish data from padding.
"""

from __future__ import annotations

from .base import AudioCodec

# CELT-only, NB, 20ms, mono, single frame
TUNNEL_TOC = 0x80

# Minimum Opus packet size to look normal (avoid suspiciously small packets)
MIN_PACKET_SIZE = 20

# Silence: Opus DTX packet (comfort noise)
# config=16, mono, code=0, single byte payload = 0
SILENCE_PACKET = bytes([TUNNEL_TOC, 0x00])

# Maximum data bytes per Opus packet (leaving room for TOC + length byte)
MAX_DATA_PER_PACKET = 250


class DirectOpusCodec(AudioCodec):
    """Mode A — pack data directly as Opus payload with valid TOC."""

    def encode(self, data: bytes) -> bytes:
        if len(data) > MAX_DATA_PER_PACKET:
            raise ValueError(
                f"Data too large for single Opus packet: "
                f"{len(data)} > {MAX_DATA_PER_PACKET}"
            )
        if len(data) == 0:
            return self.encode_silence()

        # [TOC] [length_hi] [length_lo] [data] [padding]
        length = len(data)
        packet = bytes([TUNNEL_TOC, (length >> 8) & 0xFF, length & 0xFF]) + data

        # Pad to minimum size
        if len(packet) < MIN_PACKET_SIZE:
            packet += b"\x00" * (MIN_PACKET_SIZE - len(packet))

        return packet

    def decode(self, opus_packet: bytes) -> bytes | None:
        if len(opus_packet) < 3:
            return None

        toc = opus_packet[0]
        if toc != TUNNEL_TOC:
            return None  # Not our packet (real audio or different config)

        length = (opus_packet[1] << 8) | opus_packet[2]
        if length == 0:
            return None  # Silence packet

        if length > len(opus_packet) - 3:
            return None  # Corrupted length

        return opus_packet[3 : 3 + length]

    def encode_silence(self) -> bytes:
        return SILENCE_PACKET
