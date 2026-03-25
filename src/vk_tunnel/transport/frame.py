"""
Tunnel frame protocol — the fundamental data unit transmitted over the audio channel.

Frame format:
┌───────┬───────┬────────┬──────────┬─────────┬───────┐
│ Magic │ Flags │ SeqNum │ StreamID │ Payload │ CRC16 │
│  2B   │  1B   │  4B    │   2B     │ 0-200B  │  2B   │
└───────┴───────┴────────┴──────────┴─────────┴───────┘

FEC (Reed-Solomon) is applied externally over the entire serialized frame,
so it's not part of the frame structure itself.

Total header: 9 bytes. Trailer: 2 bytes (CRC). Max payload: 200 bytes.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass, field
from enum import IntFlag

# Frame identification
MAGIC = 0xDA7A

# Header: magic(2) + flags(1) + seq(4) + stream_id(2) = 9 bytes
HEADER_SIZE = 9
# Trailer: CRC16 = 2 bytes
CRC_SIZE = 2
OVERHEAD = HEADER_SIZE + CRC_SIZE  # 11 bytes

# Max payload per frame (conservative, fits in one Opus packet with overhead)
MAX_PAYLOAD = 200

# Struct format for header: big-endian, unsigned short + byte + unsigned int + unsigned short
HEADER_FMT = ">HBIIH"  # Hmm, let me be more precise
# Actually: magic(H=2) flags(B=1) seq(I=4) stream_id(H=2) = 9 bytes
HEADER_PACK = struct.Struct(">HBIH")

# Stream ID constants
STREAM_CONTROL = 0x0000
STREAM_BROADCAST = 0xFFFF


class FrameFlags(IntFlag):
    """Frame flag bits."""
    NONE = 0x00
    ACK  = 0x80  # This frame is an acknowledgment
    SYN  = 0x40  # Stream open request
    FIN  = 0x20  # Stream close
    RST  = 0x10  # Stream reset / error
    KAL  = 0x08  # Keepalive (no payload)
    FRG  = 0x04  # Fragmented — more chunks follow

    # Encoding mode (bits 1-0)
    MODE_DIRECT = 0x00
    MODE_FSK    = 0x01


def _crc16_ccitt(data: bytes) -> int:
    """CRC-16/CCITT (polynomial 0x1021, init 0xFFFF)."""
    crc = 0xFFFF
    for byte in data:
        crc ^= byte << 8
        for _ in range(8):
            if crc & 0x8000:
                crc = (crc << 1) ^ 0x1021
            else:
                crc <<= 1
            crc &= 0xFFFF
    return crc


@dataclass(slots=True)
class Frame:
    """A single tunnel frame."""
    flags: FrameFlags
    seq: int            # 32-bit sequence number
    stream_id: int      # 16-bit stream ID
    payload: bytes = b""

    def serialize(self) -> bytes:
        """Serialize frame to bytes (without FEC — that's applied externally)."""
        if len(self.payload) > MAX_PAYLOAD:
            raise ValueError(
                f"Payload too large: {len(self.payload)} > {MAX_PAYLOAD}"
            )

        header = HEADER_PACK.pack(
            MAGIC,
            int(self.flags) & 0xFF,
            self.seq & 0xFFFFFFFF,
            self.stream_id & 0xFFFF,
        )
        body = header + self.payload
        crc = _crc16_ccitt(body)
        return body + struct.pack(">H", crc)

    @classmethod
    def deserialize(cls, data: bytes) -> Frame | None:
        """
        Deserialize bytes into a Frame.
        Returns None if magic doesn't match or CRC fails.
        """
        if len(data) < OVERHEAD:
            return None

        # Check CRC first
        body = data[:-CRC_SIZE]
        received_crc = struct.unpack(">H", data[-CRC_SIZE:])[0]
        computed_crc = _crc16_ccitt(body)
        if received_crc != computed_crc:
            return None

        # Parse header
        if len(body) < HEADER_SIZE:
            return None

        magic, flags_byte, seq, stream_id = HEADER_PACK.unpack(body[:HEADER_SIZE])
        if magic != MAGIC:
            return None

        payload = body[HEADER_SIZE:]

        return cls(
            flags=FrameFlags(flags_byte),
            seq=seq,
            stream_id=stream_id,
            payload=payload,
        )

    # -- Convenience constructors --

    @classmethod
    def data(cls, seq: int, stream_id: int, payload: bytes) -> Frame:
        return cls(flags=FrameFlags.NONE, seq=seq, stream_id=stream_id, payload=payload)

    @classmethod
    def ack(cls, seq: int, ack_seq: int, sack_ranges: list[tuple[int, int]] | None = None) -> Frame:
        """
        Create an ACK frame. Payload contains the acked seqnum (4 bytes)
        followed by optional SACK ranges (4+4 bytes each).
        """
        payload = struct.pack(">I", ack_seq)
        if sack_ranges:
            for start, end in sack_ranges:
                payload += struct.pack(">II", start, end)
        return cls(flags=FrameFlags.ACK, seq=seq, stream_id=STREAM_CONTROL, payload=payload)

    @classmethod
    def syn(cls, seq: int, stream_id: int, host: str, port: int) -> Frame:
        """Create a SYN frame to open a new stream (CONNECT request)."""
        host_bytes = host.encode("utf-8")
        payload = struct.pack(">BH", len(host_bytes), port) + host_bytes
        return cls(flags=FrameFlags.SYN, seq=seq, stream_id=stream_id, payload=payload)

    @classmethod
    def fin(cls, seq: int, stream_id: int) -> Frame:
        return cls(flags=FrameFlags.FIN, seq=seq, stream_id=stream_id)

    @classmethod
    def rst(cls, seq: int, stream_id: int) -> Frame:
        return cls(flags=FrameFlags.RST, seq=seq, stream_id=stream_id)

    @classmethod
    def keepalive(cls, seq: int) -> Frame:
        return cls(flags=FrameFlags.KAL, seq=seq, stream_id=STREAM_CONTROL)

    # -- Payload parsers --

    def parse_ack(self) -> tuple[int, list[tuple[int, int]]]:
        """Parse ACK payload → (cumulative_ack, sack_ranges)."""
        if len(self.payload) < 4:
            raise ValueError("ACK payload too short")
        ack_seq = struct.unpack(">I", self.payload[:4])[0]
        sack_ranges = []
        offset = 4
        while offset + 8 <= len(self.payload):
            start, end = struct.unpack(">II", self.payload[offset : offset + 8])
            sack_ranges.append((start, end))
            offset += 8
        return ack_seq, sack_ranges

    def parse_syn(self) -> tuple[str, int]:
        """Parse SYN payload → (host, port)."""
        if len(self.payload) < 3:
            raise ValueError("SYN payload too short")
        host_len, port = struct.unpack(">BH", self.payload[:3])
        host = self.payload[3 : 3 + host_len].decode("utf-8")
        return host, port

    @property
    def is_ack(self) -> bool:
        return bool(self.flags & FrameFlags.ACK)

    @property
    def is_syn(self) -> bool:
        return bool(self.flags & FrameFlags.SYN)

    @property
    def is_fin(self) -> bool:
        return bool(self.flags & FrameFlags.FIN)

    @property
    def is_rst(self) -> bool:
        return bool(self.flags & FrameFlags.RST)

    @property
    def is_keepalive(self) -> bool:
        return bool(self.flags & FrameFlags.KAL)

    def __repr__(self) -> str:
        flag_names = []
        for f in (FrameFlags.ACK, FrameFlags.SYN, FrameFlags.FIN,
                  FrameFlags.RST, FrameFlags.KAL, FrameFlags.FRG):
            if self.flags & f:
                flag_names.append(f.name)
        flags_str = "|".join(flag_names) if flag_names else "DATA"
        return (
            f"Frame({flags_str} seq={self.seq} stream={self.stream_id:#06x} "
            f"payload={len(self.payload)}B)"
        )
