"""Tests for frame serialization/deserialization."""

import pytest
from vk_tunnel.transport.frame import Frame, FrameFlags, MAGIC, MAX_PAYLOAD


class TestFrameSerialization:
    def test_data_frame_roundtrip(self):
        frame = Frame.data(seq=42, stream_id=0x0001, payload=b"hello world")
        raw = frame.serialize()
        restored = Frame.deserialize(raw)

        assert restored is not None
        assert restored.seq == 42
        assert restored.stream_id == 0x0001
        assert restored.payload == b"hello world"
        assert not restored.is_ack
        assert not restored.is_syn

    def test_ack_frame_roundtrip(self):
        frame = Frame.ack(seq=1, ack_seq=41, sack_ranges=[(45, 47), (50, 52)])
        raw = frame.serialize()
        restored = Frame.deserialize(raw)

        assert restored is not None
        assert restored.is_ack
        ack_seq, sack_ranges = restored.parse_ack()
        assert ack_seq == 41
        assert sack_ranges == [(45, 47), (50, 52)]

    def test_syn_frame_roundtrip(self):
        frame = Frame.syn(seq=0, stream_id=0x0042, host="example.com", port=443)
        raw = frame.serialize()
        restored = Frame.deserialize(raw)

        assert restored is not None
        assert restored.is_syn
        host, port = restored.parse_syn()
        assert host == "example.com"
        assert port == 443

    def test_fin_frame(self):
        frame = Frame.fin(seq=100, stream_id=5)
        raw = frame.serialize()
        restored = Frame.deserialize(raw)

        assert restored is not None
        assert restored.is_fin
        assert restored.stream_id == 5

    def test_rst_frame(self):
        frame = Frame.rst(seq=100, stream_id=5)
        raw = frame.serialize()
        restored = Frame.deserialize(raw)

        assert restored is not None
        assert restored.is_rst

    def test_keepalive_frame(self):
        frame = Frame.keepalive(seq=99)
        raw = frame.serialize()
        restored = Frame.deserialize(raw)

        assert restored is not None
        assert restored.is_keepalive
        assert restored.payload == b""

    def test_empty_payload(self):
        frame = Frame.data(seq=0, stream_id=1, payload=b"")
        raw = frame.serialize()
        restored = Frame.deserialize(raw)

        assert restored is not None
        assert restored.payload == b""

    def test_max_payload(self):
        payload = bytes(range(256)) * (MAX_PAYLOAD // 256) + bytes(range(MAX_PAYLOAD % 256))
        payload = payload[:MAX_PAYLOAD]
        frame = Frame.data(seq=0, stream_id=1, payload=payload)
        raw = frame.serialize()
        restored = Frame.deserialize(raw)

        assert restored is not None
        assert restored.payload == payload

    def test_payload_too_large(self):
        with pytest.raises(ValueError, match="Payload too large"):
            Frame.data(seq=0, stream_id=1, payload=b"x" * (MAX_PAYLOAD + 1)).serialize()

    def test_corrupted_crc_rejected(self):
        frame = Frame.data(seq=1, stream_id=1, payload=b"test")
        raw = bytearray(frame.serialize())
        raw[-1] ^= 0xFF  # Flip CRC bits
        assert Frame.deserialize(bytes(raw)) is None

    def test_corrupted_payload_rejected(self):
        frame = Frame.data(seq=1, stream_id=1, payload=b"test data here")
        raw = bytearray(frame.serialize())
        raw[10] ^= 0xFF  # Flip a payload byte
        assert Frame.deserialize(bytes(raw)) is None

    def test_wrong_magic_rejected(self):
        frame = Frame.data(seq=1, stream_id=1, payload=b"test")
        raw = bytearray(frame.serialize())
        raw[0] = 0x00  # Break magic
        raw[1] = 0x00
        assert Frame.deserialize(bytes(raw)) is None

    def test_too_short_data_rejected(self):
        assert Frame.deserialize(b"") is None
        assert Frame.deserialize(b"\x00" * 5) is None

    def test_seq_wraparound(self):
        frame = Frame.data(seq=0xFFFFFFFF, stream_id=1, payload=b"wrap")
        raw = frame.serialize()
        restored = Frame.deserialize(raw)
        assert restored is not None
        assert restored.seq == 0xFFFFFFFF

    def test_all_stream_ids(self):
        for sid in [0x0000, 0x0001, 0x7FFF, 0xFFFE, 0xFFFF]:
            frame = Frame.data(seq=0, stream_id=sid, payload=b"x")
            raw = frame.serialize()
            restored = Frame.deserialize(raw)
            assert restored is not None
            assert restored.stream_id == sid

    def test_combined_flags(self):
        frame = Frame(
            flags=FrameFlags.SYN | FrameFlags.ACK,
            seq=10,
            stream_id=5,
            payload=b"",
        )
        raw = frame.serialize()
        restored = Frame.deserialize(raw)
        assert restored is not None
        assert restored.is_syn
        assert restored.is_ack

    def test_repr(self):
        frame = Frame.data(seq=1, stream_id=1, payload=b"hello")
        r = repr(frame)
        assert "DATA" in r
        assert "seq=1" in r


class TestFrameFEC:
    """Test frame + FEC integration."""

    def test_frame_survives_fec(self):
        from vk_tunnel.codec.fec import FECCodec

        fec = FECCodec(nsym=16)
        frame = Frame.data(seq=42, stream_id=1, payload=b"test data via FEC")

        raw = frame.serialize()
        encoded = fec.encode(raw)
        decoded = fec.decode(encoded)

        assert decoded is not None
        restored = Frame.deserialize(decoded)
        assert restored is not None
        assert restored.payload == b"test data via FEC"

    def test_frame_survives_fec_with_errors(self):
        from vk_tunnel.codec.fec import FECCodec

        fec = FECCodec(nsym=16)
        frame = Frame.data(seq=42, stream_id=1, payload=b"corrupted data test")

        raw = frame.serialize()
        encoded = bytearray(fec.encode(raw))

        # Introduce 5 byte errors (FEC can correct up to 8)
        for i in range(5):
            encoded[i + 3] ^= 0xFF

        decoded = fec.decode(bytes(encoded))
        assert decoded is not None

        restored = Frame.deserialize(decoded)
        assert restored is not None
        assert restored.payload == b"corrupted data test"

    def test_frame_fails_fec_too_many_errors(self):
        from vk_tunnel.codec.fec import FECCodec

        fec = FECCodec(nsym=16)
        frame = Frame.data(seq=42, stream_id=1, payload=b"too many errors")

        raw = frame.serialize()
        encoded = bytearray(fec.encode(raw))

        # Introduce 20 byte errors (FEC can only correct 8)
        for i in range(20):
            encoded[i] ^= 0xFF

        decoded = fec.decode(bytes(encoded))
        assert decoded is None
