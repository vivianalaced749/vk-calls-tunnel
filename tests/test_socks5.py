"""Tests for SOCKS5 protocol parsing."""

import asyncio
import struct
import pytest


class TestSocks5Protocol:
    """Test SOCKS5 greeting and request parsing without full tunnel stack."""

    def test_greeting_format(self):
        """Verify correct SOCKS5 greeting bytes."""
        # Client greeting: VER=5, NMETHODS=1, METHODS=[0x00 (no auth)]
        greeting = bytes([0x05, 0x01, 0x00])
        assert greeting[0] == 0x05  # SOCKS5
        assert greeting[1] == 1     # 1 method
        assert greeting[2] == 0x00  # No auth

    def test_connect_request_domain(self):
        """Verify SOCKS5 CONNECT request with domain name."""
        host = "example.com"
        port = 443
        # VER=5, CMD=1 (CONNECT), RSV=0, ATYP=3 (domain)
        request = bytes([0x05, 0x01, 0x00, 0x03])
        request += bytes([len(host)]) + host.encode()
        request += struct.pack("!H", port)

        # Parse it back
        assert request[0] == 0x05
        assert request[1] == 0x01  # CONNECT
        assert request[3] == 0x03  # Domain
        domain_len = request[4]
        domain = request[5:5 + domain_len].decode()
        parsed_port = struct.unpack("!H", request[5 + domain_len:])[0]

        assert domain == "example.com"
        assert parsed_port == 443

    def test_connect_request_ipv4(self):
        """Verify SOCKS5 CONNECT request with IPv4 address."""
        # VER=5, CMD=1, RSV=0, ATYP=1 (IPv4)
        request = bytes([0x05, 0x01, 0x00, 0x01])
        request += bytes([93, 184, 216, 34])  # 93.184.216.34
        request += struct.pack("!H", 80)

        assert request[3] == 0x01  # IPv4
        ip = ".".join(str(b) for b in request[4:8])
        port = struct.unpack("!H", request[8:10])[0]

        assert ip == "93.184.216.34"
        assert port == 80

    def test_success_reply_format(self):
        """Verify SOCKS5 success reply format."""
        # VER=5, REP=0 (success), RSV=0, ATYP=1, BND.ADDR=0.0.0.0, BND.PORT=0
        reply = bytes([0x05, 0x00, 0x00, 0x01, 0, 0, 0, 0, 0, 0])
        assert reply[0] == 0x05
        assert reply[1] == 0x00  # Success
        assert len(reply) == 10
