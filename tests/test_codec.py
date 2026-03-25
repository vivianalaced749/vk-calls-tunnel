"""Tests for audio codecs."""

import pytest
from vk_tunnel.codec.direct_opus import DirectOpusCodec, MAX_DATA_PER_PACKET, TUNNEL_TOC


class TestDirectOpusCodec:
    def setup_method(self):
        self.codec = DirectOpusCodec()

    def test_encode_decode_roundtrip(self):
        data = b"hello tunnel"
        packet = self.codec.encode(data)
        decoded = self.codec.decode(packet)
        assert decoded == data

    def test_encode_decode_binary(self):
        data = bytes(range(200))  # Binary data within size limit
        packet = self.codec.encode(data)
        decoded = self.codec.decode(packet)
        assert decoded == data

    def test_encode_decode_single_byte(self):
        data = b"\x42"
        packet = self.codec.encode(data)
        decoded = self.codec.decode(packet)
        assert decoded == data

    def test_encode_decode_max_size(self):
        data = b"\xAB" * MAX_DATA_PER_PACKET
        packet = self.codec.encode(data)
        decoded = self.codec.decode(packet)
        assert decoded == data

    def test_encode_too_large(self):
        with pytest.raises(ValueError, match="Data too large"):
            self.codec.encode(b"\x00" * (MAX_DATA_PER_PACKET + 1))

    def test_empty_data_returns_silence(self):
        packet = self.codec.encode(b"")
        decoded = self.codec.decode(packet)
        assert decoded is None  # Silence = no data

    def test_silence_packet(self):
        packet = self.codec.encode_silence()
        decoded = self.codec.decode(packet)
        assert decoded is None

    def test_wrong_toc_returns_none(self):
        # Packet with different TOC byte (not our tunnel)
        packet = bytes([0x00, 0x00, 0x05]) + b"hello"
        decoded = self.codec.decode(packet)
        assert decoded is None

    def test_short_packet_returns_none(self):
        assert self.codec.decode(b"") is None
        assert self.codec.decode(b"\x80") is None
        assert self.codec.decode(b"\x80\x00") is None

    def test_corrupted_length_returns_none(self):
        # Length says 100 bytes but only 5 available
        packet = bytes([TUNNEL_TOC, 0x00, 100]) + b"hello"
        decoded = self.codec.decode(packet)
        assert decoded is None

    def test_packet_has_valid_toc(self):
        data = b"test"
        packet = self.codec.encode(data)
        assert packet[0] == TUNNEL_TOC

    def test_minimum_packet_size(self):
        data = b"x"
        packet = self.codec.encode(data)
        assert len(packet) >= 20  # MIN_PACKET_SIZE


class TestFECCodec:
    def test_encode_decode_roundtrip(self):
        from vk_tunnel.codec.fec import FECCodec
        fec = FECCodec(nsym=16)

        data = b"reed solomon test data"
        encoded = fec.encode(data)
        decoded = fec.decode(encoded)
        assert decoded == data

    def test_parity_size(self):
        from vk_tunnel.codec.fec import FECCodec
        fec = FECCodec(nsym=16)
        assert fec.parity_size == 16

        data = b"test"
        encoded = fec.encode(data)
        assert len(encoded) == len(data) + 16

    def test_error_correction(self):
        from vk_tunnel.codec.fec import FECCodec
        fec = FECCodec(nsym=16)

        data = b"correct me if you can"
        encoded = bytearray(fec.encode(data))

        # Corrupt 7 bytes (within correction capability)
        for i in [0, 3, 7, 11, 15, 19, 22]:
            encoded[i] ^= 0xFF

        decoded = fec.decode(bytes(encoded))
        assert decoded == data

    def test_uncorrectable_errors(self):
        from vk_tunnel.codec.fec import FECCodec
        fec = FECCodec(nsym=4)  # Only 2 error correction

        data = b"fragile data"
        encoded = bytearray(fec.encode(data))

        # Corrupt 10 bytes (way beyond correction)
        for i in range(10):
            encoded[i] ^= 0xFF

        decoded = fec.decode(bytes(encoded))
        assert decoded is None
