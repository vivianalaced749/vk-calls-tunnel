"""
CLI entry point: python -m vk_tunnel

Usage:
    python -m vk_tunnel client   # Run tunnel client (inside Russia)
    python -m vk_tunnel bot      # Run tunnel bot (VPS outside Russia)
"""

from __future__ import annotations

import argparse
import asyncio
import sys

import structlog

structlog.configure(
    processors=[
        structlog.stdlib.add_log_level,
        structlog.dev.ConsoleRenderer(colors=True),
    ],
    wrapper_class=structlog.stdlib.BoundLogger,
    logger_factory=structlog.PrintLoggerFactory(),
)


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="vk-tunnel",
        description="Data tunnel through VK voice calls",
    )
    parser.add_argument(
        "role",
        choices=["client", "bot"],
        help="Run as 'client' (inside Russia) or 'bot' (VPS outside Russia)",
    )
    parser.add_argument("--vk-login", help="VK phone number or email")
    parser.add_argument("--vk-password", help="VK password")
    parser.add_argument("--vk-token", help="VK API token (alternative to login/password)")
    parser.add_argument("--peer-id", type=int, help="VK user ID of the other side")
    parser.add_argument("--socks-host", default="127.0.0.1", help="SOCKS5 bind host (client only)")
    parser.add_argument("--socks-port", type=int, default=1080, help="SOCKS5 bind port (client only)")

    args = parser.parse_args()

    from .config import TunnelConfig

    config = TunnelConfig(
        vk_login=args.vk_login or "",
        vk_password=args.vk_password or "",
        vk_token=args.vk_token or "",
        vk_peer_id=args.peer_id or 0,
        socks5_host=args.socks_host,
        socks5_port=args.socks_port,
    )

    if args.role == "client":
        from .client.main import run_client
        asyncio.run(run_client(config))
    else:
        from .bot.main import run_bot
        asyncio.run(run_bot(config))


if __name__ == "__main__":
    main()
