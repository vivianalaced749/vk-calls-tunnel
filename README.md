# VK Calls Tunnel

Data tunnel through VK voice calls. Encodes data into Opus audio frames, transmits via VK's WebRTC media servers.

VK media server IPs are guaranteed whitelisted — making this one of the most resilient methods to bypass IP whitelists in Russia.

## How it works

```
Client (Russia) → VK call (whitelisted media servers) → Bot on the other end → Decode → Internet
```

1. Bot (Python) holds a VK account, waits for incoming calls
2. Client initiates VK voice call to the bot
3. WebRTC connection established through VK media servers
4. Client encodes data into Opus audio frames (48kHz) and sends as audio stream
5. Bot receives audio, decodes data, proxies requests to the internet
6. Response encoded back into audio, sent to client
7. Client decodes — data received

DPI sees a normal VK voice call.

## Quick start

### Bot (VPS outside Russia)

```bash
pip install -e .
playwright install chromium

python -m vk_tunnel bot \
    --vk-login "+79001234567" \
    --vk-password "password" \
    --peer-id 123456789
```

### Client (inside Russia)

```bash
pip install -e .
playwright install chromium

python -m vk_tunnel client \
    --vk-login "+79009876543" \
    --vk-password "password" \
    --peer-id 987654321 \
    --socks-port 1080
```

Then configure your apps to use SOCKS5 proxy at `127.0.0.1:1080`.

```bash
# Test with curl
curl --socks5 127.0.0.1:1080 https://ifconfig.me

# Or set system-wide
export ALL_PROXY=socks5://127.0.0.1:1080
```

## Performance

| Mode | Throughput | Latency | Survives transcoding |
|------|-----------|---------|---------------------|
| **Mode A** (direct Opus payload) | ~70 kbps | ~200ms | No |
| **Mode B** (FSK modulation) | ~4 kbps | ~300ms | Yes |

Mode A is used by default; auto-fallback to Mode B if VK transcodes.

### What works
- Text messengers (Telegram, Signal)
- Email
- Web pages (slow)

### What doesn't
- Video streaming
- Normal browsing speed
- Large downloads

## Why this is resilient

VK calls use media servers with IPs that are **guaranteed** in any Russian whitelist. Blocking them = disabling VK calls for the entire country. RKN won't do that.

Unlike cloud IP fishing (Yandex Cloud, Cloud.ru) where you gamble on getting a whitelisted IP, VK media servers are always whitelisted by definition.

## Architecture

```
CLIENT (Russia)                              BOT (VPS abroad)
===============                              ================

┌──────────────┐                             ┌──────────────┐
│  App (browser,│                             │  Internet    │
│  Telegram)   │                             │  (target     │
│              │                             │   servers)   │
└──────┬───────┘                             └──────▲───────┘
       │ SOCKS5                                     │ TCP
┌──────▼───────┐                             ┌──────┴───────┐
│  Multiplexer │                             │  Proxy       │
│  (streams)   │                             │  Engine      │
├──────────────┤                             ├──────────────┤
│  Transport   │                             │  Transport   │
│  (reliable)  │                             │  (reliable)  │
├──────────────┤                             ├──────────────┤
│  FEC + Codec │    VK Call (WebRTC/SRTP)    │  FEC + Codec │
│  (data→audio)│◄═══════════════════════════►│  (audio→data)│
├──────────────┤    via VK Media Servers     ├──────────────┤
│  Playwright  │    (whitelisted IPs)        │  Playwright  │
│  (browser)   │                             │  (browser)   │
└──────────────┘                             └──────────────┘
```

### Components

- **SOCKS5 Server** — local proxy, apps connect here transparently
- **Multiplexer** — maps multiple TCP connections onto one audio stream
- **Transport** — sliding window, ACK, retransmission, congestion control
- **FEC** — Reed-Solomon error correction (16 parity bytes, corrects 8 byte errors)
- **Codec** — Mode A: direct Opus payload replacement / Mode B: FSK modulation
- **Playwright Bridge** — headless Chromium, hooks WebRTC to inject/capture audio

### Frame Protocol

```
┌───────┬───────┬────────┬──────────┬─────────┬───────┐
│ Magic │ Flags │ SeqNum │ StreamID │ Payload │ CRC16 │
│  2B   │  1B   │  4B    │   2B     │ 0-200B  │  2B   │
└───────┴───────┴────────┴──────────┴─────────┴───────┘
+ Reed-Solomon FEC (16 bytes) applied externally
```

## Project structure

```
src/vk_tunnel/
├── __main__.py          # CLI entry point
├── config.py            # Pydantic settings
├── client/
│   ├── main.py          # Client stack (SOCKS5 + tunnel)
│   └── socks5_server.py # RFC 1928 SOCKS5 proxy
├── bot/
│   ├── main.py          # Bot stack (proxy + tunnel)
│   └── proxy_engine.py  # TCP proxy to internet
├── transport/
│   ├── frame.py         # Frame serialization (magic, CRC, flags)
│   ├── protocol.py      # Sliding window, ACK, retransmit
│   └── mux.py           # Stream multiplexer
├── codec/
│   ├── base.py          # Abstract codec interface
│   ├── direct_opus.py   # Mode A: Opus payload replacement
│   └── fec.py           # Reed-Solomon FEC
└── vk/
    ├── auth.py           # VK login/session
    ├── call_manager.py   # Call lifecycle state machine
    └── browser_bridge.py # Playwright WebRTC interception
```

## Configuration

All settings via environment variables with `VKT_` prefix:

```bash
VKT_VK_LOGIN="+79001234567"
VKT_VK_PASSWORD="password"
VKT_VK_PEER_ID=123456789
VKT_SOCKS5_PORT=1080
VKT_ENCODING_MODE=auto    # auto/direct/fsk
VKT_WINDOW_SIZE=32
VKT_FEC_PARITY_SYMBOLS=16
```

## Development

```bash
python -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
pytest
```

## Status

Core tunnel stack implemented. Next steps:
- [ ] Test with real VK accounts
- [ ] Mode B (FSK) codec implementation
- [ ] Mode auto-detection probing
- [ ] Multi-account rotation
- [ ] End-to-end encryption (AES-256-GCM)

## Related

- [vpn-gcloud](https://github.com/kobzevvv/vpn-gcloud) — VLESS+Reality deployment, whitelist architecture
- [Xray-core](https://github.com/XTLS/Xray-core) — VLESS+Reality protocol
- [ntc.party](https://ntc.party) — Censorship bypass community

## Author

[@kobzevvv](https://twitter.com/kobzevvv)
