from __future__ import annotations

from enum import Enum
from pydantic import Field
from pydantic_settings import BaseSettings


class EncodingMode(str, Enum):
    DIRECT = "direct"  # Mode A: direct Opus payload replacement (~70 kbps)
    FSK = "fsk"        # Mode B: FSK modulation through Opus (~4 kbps)
    AUTO = "auto"      # Auto-detect: probe then choose


class TunnelConfig(BaseSettings):
    model_config = {"env_prefix": "VKT_"}

    # VK credentials
    vk_login: str = ""
    vk_password: str = ""
    vk_token: str = ""
    vk_peer_id: int = 0  # VK user ID of the other side

    # Network
    socks5_host: str = "127.0.0.1"
    socks5_port: int = 1080

    # Encoding
    encoding_mode: EncodingMode = EncodingMode.AUTO
    opus_frame_duration_ms: int = 20  # 20ms Opus frames
    opus_sample_rate: int = 48000

    # Transport
    window_size: int = 32          # Sliding window size (frames)
    retransmit_timeout_ms: int = 500
    max_retransmits: int = 10
    keepalive_interval_s: float = 5.0
    link_dead_timeout_s: float = 15.0

    # Reconnection
    max_reconnect_attempts: int = 10
    reconnect_backoff_base_s: float = 2.0
    reconnect_backoff_max_s: float = 60.0
    session_timeout_s: float = 30.0

    # Anti-detection
    call_rotation_min_m: int = 15
    call_rotation_max_m: int = 45

    # FEC
    fec_parity_symbols: int = 16  # Reed-Solomon parity bytes

    # Multiplexer
    max_streams: int = 256
    stream_scheduler_quantum: int = 4  # Chunks per stream before round-robin yield

    # Shared secret for session auth (hex-encoded)
    shared_secret: str = ""

    @property
    def opus_frame_samples(self) -> int:
        return self.opus_sample_rate * self.opus_frame_duration_ms // 1000

    @property
    def frames_per_second(self) -> int:
        return 1000 // self.opus_frame_duration_ms
