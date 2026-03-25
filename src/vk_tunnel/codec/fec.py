"""
Reed-Solomon Forward Error Correction wrapper.

Adds RS parity symbols to each frame before encoding into audio,
and corrects errors after decoding. This is essential because:
- Mode B (FSK) goes through a lossy Opus encode/decode cycle
- Even Mode A may experience bit flips if VK servers touch the packets
- VoIP networks can corrupt/drop individual bytes

Default: 16 parity symbols → can correct 8 byte errors or 16 erasures per frame.
"""

from __future__ import annotations

from reedsolo import RSCodec, ReedSolomonError


class FECCodec:
    """Reed-Solomon encoder/decoder for tunnel frames."""

    def __init__(self, nsym: int = 16):
        self._nsym = nsym
        self._codec = RSCodec(nsym)

    @property
    def parity_size(self) -> int:
        return self._nsym

    def encode(self, data: bytes) -> bytes:
        """Add RS parity symbols to data."""
        return bytes(self._codec.encode(data))

    def decode(self, data: bytes) -> bytes | None:
        """
        Decode and correct errors in data.
        Returns corrected data (without parity) or None if uncorrectable.
        """
        try:
            decoded_msg, _, _ = self._codec.decode(data)
            return bytes(decoded_msg)
        except ReedSolomonError:
            return None
