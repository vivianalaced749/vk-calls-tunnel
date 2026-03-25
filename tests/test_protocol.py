"""Tests for transport protocol."""

import asyncio
import pytest
from vk_tunnel.transport.frame import Frame, FrameFlags
from vk_tunnel.transport.protocol import TransportProtocol


class TestTransportProtocol:
    """Test the transport protocol with a simulated loopback."""

    @pytest.fixture
    async def loopback(self):
        """Create two connected transport protocols (client + server)."""
        received_data: dict[str, list] = {"client": [], "server": []}

        async def client_send(frame: Frame):
            """Client sends → server receives."""
            await server.receive_frame(frame)

        async def server_send(frame: Frame):
            """Server sends → client receives."""
            await client.receive_frame(frame)

        client = TransportProtocol(
            send_callback=client_send,
            window_size=8,
            retransmit_timeout_ms=200,
            keepalive_interval=100.0,  # Disable for tests
            link_dead_timeout=100.0,
        )

        server = TransportProtocol(
            send_callback=server_send,
            window_size=8,
            retransmit_timeout_ms=200,
            keepalive_interval=100.0,
            link_dead_timeout=100.0,
        )

        async def client_recv(stream_id, data):
            received_data["client"].append((stream_id, data))

        async def server_recv(stream_id, data):
            received_data["server"].append((stream_id, data))

        client.on_receive = client_recv
        server.on_receive = server_recv

        await client.start()
        await server.start()

        yield client, server, received_data

        await client.stop()
        await server.stop()

    @pytest.mark.asyncio
    async def test_send_receive(self, loopback):
        client, server, received = loopback

        await client.send(1, b"hello from client")
        await asyncio.sleep(0.1)

        assert len(received["server"]) > 0
        # Reassemble all received chunks
        data = b"".join(d for _, d in received["server"])
        assert data == b"hello from client"

    @pytest.mark.asyncio
    async def test_bidirectional(self, loopback):
        client, server, received = loopback

        await client.send(1, b"ping")
        await asyncio.sleep(0.2)

        await server.send(1, b"pong")
        await asyncio.sleep(0.3)

        server_data = b"".join(d for _, d in received["server"])
        client_data = b"".join(d for _, d in received["client"])

        assert server_data == b"ping"
        assert client_data == b"pong"

    @pytest.mark.asyncio
    async def test_large_data_chunking(self, loopback):
        client, server, received = loopback

        # Send more data than fits in one frame
        payload = b"X" * 1000
        await client.send(1, payload)
        await asyncio.sleep(0.3)

        data = b"".join(d for _, d in received["server"])
        assert data == payload

    @pytest.mark.asyncio
    async def test_stats_tracking(self, loopback):
        client, server, received = loopback

        await client.send(1, b"stats test")
        await asyncio.sleep(0.1)

        assert client.stats.frames_sent > 0
        assert client.stats.bytes_sent > 0
        assert server.stats.frames_received > 0

    @pytest.mark.asyncio
    async def test_control_frame(self, loopback):
        client, server, received = loopback

        control_frames = []
        async def on_control(frame):
            control_frames.append(frame)
        server.on_control = on_control

        syn = Frame.syn(seq=0, stream_id=0x0042, host="example.com", port=443)
        await client.send_control(syn)
        await asyncio.sleep(0.1)

        assert len(control_frames) == 1
        assert control_frames[0].is_syn
        host, port = control_frames[0].parse_syn()
        assert host == "example.com"
        assert port == 443
