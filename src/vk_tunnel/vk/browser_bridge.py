"""
Playwright-based WebRTC bridge for VK calls.

Uses a headless Chromium browser to:
1. Login to VK
2. Initiate/answer voice calls
3. Intercept WebRTC RTCPeerConnection
4. Replace outgoing audio track with tunnel data
5. Capture incoming audio track and extract tunnel data

This approach avoids reverse-engineering VK's proprietary signaling protocol.
The JavaScript injection hooks standard WebRTC APIs that any browser uses.
"""

from __future__ import annotations

import asyncio
import base64
import json

import structlog
from playwright.async_api import async_playwright, Page, Browser, BrowserContext

from ..config import TunnelConfig
from .call_manager import CallManager, CallState

log = structlog.get_logger()

# JavaScript to inject into the page — intercepts WebRTC
WEBRTC_HOOK_JS = """
(() => {
    // Store reference to original RTCPeerConnection
    const OriginalRTC = window.RTCPeerConnection;

    // Tunnel state
    window.__vkTunnel = {
        pc: null,
        dataChannel: null,
        outgoingQueue: [],
        incomingQueue: [],
        audioContext: null,
        sender: null,
        ready: false,
    };

    // Hook RTCPeerConnection constructor
    window.RTCPeerConnection = function(...args) {
        const pc = new OriginalRTC(...args);
        window.__vkTunnel.pc = pc;

        console.log('[vk-tunnel] RTCPeerConnection created');

        // Hook addTrack to intercept audio
        const origAddTrack = pc.addTrack.bind(pc);
        pc.addTrack = function(track, ...streams) {
            if (track.kind === 'audio') {
                console.log('[vk-tunnel] Intercepting audio track');

                // Create AudioContext for generating custom audio
                const ctx = new AudioContext({ sampleRate: 48000 });
                window.__vkTunnel.audioContext = ctx;

                // Create a ScriptProcessorNode (or AudioWorklet) for custom audio
                // ScriptProcessorNode is deprecated but simpler for PoC
                const bufferSize = 960; // 20ms at 48kHz
                const processor = ctx.createScriptProcessor(bufferSize, 1, 1);

                processor.onaudioprocess = (e) => {
                    const output = e.outputBuffer.getChannelData(0);
                    const queued = window.__vkTunnel.outgoingQueue.shift();

                    if (queued) {
                        // Decode base64 PCM samples into float32 audio buffer
                        const bytes = Uint8Array.from(atob(queued), c => c.charCodeAt(0));
                        const float32 = new Float32Array(bytes.buffer);
                        for (let i = 0; i < output.length && i < float32.length; i++) {
                            output[i] = float32[i];
                        }
                    } else {
                        // Silence when no data queued
                        output.fill(0);
                    }
                };

                // Connect processor -> destination to keep it alive
                const dest = ctx.createMediaStreamDestination();
                processor.connect(dest);
                // Also connect to ctx.destination to prevent GC
                const silentGain = ctx.createGain();
                silentGain.gain.value = 0;
                processor.connect(silentGain);
                silentGain.connect(ctx.destination);
                // Need an input source to trigger onaudioprocess
                const osc = ctx.createOscillator();
                osc.frequency.value = 0;
                osc.connect(processor);
                osc.start();

                // Replace the original audio track with our custom one
                const customTrack = dest.stream.getAudioTracks()[0];
                const sender = origAddTrack(customTrack, ...streams);
                window.__vkTunnel.sender = sender;

                console.log('[vk-tunnel] Audio track replaced with tunnel track');
                return sender;
            }
            return origAddTrack(track, ...streams);
        };

        // Hook ontrack to capture incoming audio
        const origOnTrack = Object.getOwnPropertyDescriptor(
            RTCPeerConnection.prototype, 'ontrack'
        );

        pc.addEventListener('track', (event) => {
            if (event.track.kind === 'audio') {
                console.log('[vk-tunnel] Incoming audio track detected');

                const ctx = window.__vkTunnel.audioContext ||
                            new AudioContext({ sampleRate: 48000 });
                const source = ctx.createMediaStreamSource(
                    new MediaStream([event.track])
                );

                const bufferSize = 960;
                const captureProcessor = ctx.createScriptProcessor(bufferSize, 1, 1);

                captureProcessor.onaudioprocess = (e) => {
                    const input = e.inputBuffer.getChannelData(0);
                    // Convert float32 to bytes and base64 encode
                    const bytes = new Uint8Array(input.buffer.slice(
                        input.byteOffset,
                        input.byteOffset + input.byteLength
                    ));
                    const b64 = btoa(String.fromCharCode(...bytes));
                    window.__vkTunnel.incomingQueue.push(b64);

                    // Keep queue bounded
                    while (window.__vkTunnel.incomingQueue.length > 100) {
                        window.__vkTunnel.incomingQueue.shift();
                    }
                };

                source.connect(captureProcessor);
                // Connect to silent destination to keep alive
                const silentGain = ctx.createGain();
                silentGain.gain.value = 0;
                captureProcessor.connect(silentGain);
                silentGain.connect(ctx.destination);
            }
        });

        // Monitor connection state
        pc.addEventListener('connectionstatechange', () => {
            console.log('[vk-tunnel] Connection state:', pc.connectionState);
            if (pc.connectionState === 'connected') {
                window.__vkTunnel.ready = true;
            } else if (pc.connectionState === 'failed' ||
                       pc.connectionState === 'disconnected') {
                window.__vkTunnel.ready = false;
            }
        });

        return pc;
    };

    // Copy static properties
    Object.keys(OriginalRTC).forEach(key => {
        window.RTCPeerConnection[key] = OriginalRTC[key];
    });
    window.RTCPeerConnection.prototype = OriginalRTC.prototype;

    console.log('[vk-tunnel] WebRTC hooks installed');
})();
"""


class BrowserBridge(CallManager):
    """
    Playwright-based VK call bridge.

    Launches headless Chromium, logs into VK, hooks WebRTC,
    and provides send/receive for Opus audio frames.
    """

    def __init__(self, config: TunnelConfig):
        super().__init__(config)
        self._playwright = None
        self._browser: Browser | None = None
        self._context: BrowserContext | None = None
        self._page: Page | None = None
        self._poll_task: asyncio.Task | None = None

    async def launch(self) -> None:
        """Launch browser and login to VK."""
        self._playwright = await async_playwright().start()
        self._browser = await self._playwright.chromium.launch(
            headless=True,
            args=[
                "--use-fake-ui-for-media-stream",     # Auto-allow mic/camera
                "--use-fake-device-for-media-stream",  # Fake media devices
                "--disable-web-security",
                "--autoplay-policy=no-user-gesture-required",
            ],
        )
        self._context = await self._browser.new_context(
            permissions=["microphone"],
            user_agent=(
                "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
            ),
        )
        self._page = await self._context.new_page()

        # Inject WebRTC hooks before any page loads
        await self._page.add_init_script(WEBRTC_HOOK_JS)

        # Login to VK
        await self._login()

    async def _login(self) -> None:
        """Login to VK via the web interface."""
        page = self._page
        log.info("vk login: navigating to login page")

        await page.goto("https://vk.com/login")
        await page.wait_for_load_state("networkidle")

        # Check if already logged in
        if "feed" in page.url or "id" in page.url:
            log.info("vk login: already logged in")
            return

        # Fill login form
        if self._config.vk_login:
            # VK login flow: phone input → next → password
            await page.fill('input[name="login"]', self._config.vk_login)
            await page.click('button[type="submit"]')
            await page.wait_for_timeout(2000)

            # Password step
            await page.fill('input[name="password"]', self._config.vk_password)
            await page.click('button[type="submit"]')
            await page.wait_for_timeout(3000)

            if "login" in page.url:
                # May need 2FA
                log.warning("vk login: may need 2FA — check browser")

            log.info("vk login: complete", url=page.url)

    async def start_call(self) -> None:
        """Initiate a VK voice call to the configured peer."""
        await self._set_state(CallState.SIGNALING)

        peer_id = self._config.vk_peer_id
        page = self._page

        # Navigate to the peer's message page and initiate call
        await page.goto(f"https://vk.com/im?sel={peer_id}")
        await page.wait_for_load_state("networkidle")
        await page.wait_for_timeout(1000)

        # Click the call button in the chat header
        # VK UI: the phone icon in the top-right of the conversation
        call_button = page.locator('[data-testid="call-button"], .im-page--toolbox .phone-icon, button[aria-label*="звон"], button[aria-label*="call"]')

        try:
            await call_button.first.click(timeout=5000)
            log.info("call: initiated", peer_id=peer_id)
        except Exception:
            # Fallback: try VK API call via page evaluation
            log.info("call: trying API-based call initiation")
            await page.evaluate(f"""
                fetch('/al_calls.php', {{
                    method: 'POST',
                    headers: {{'Content-Type': 'application/x-www-form-urlencoded'}},
                    body: 'act=call&peer_id={peer_id}&type=audio'
                }});
            """)

        # Wait for WebRTC connection
        await self._wait_for_connection()

    async def answer_call(self) -> None:
        """Wait for and answer an incoming VK call (bot side)."""
        await self._set_state(CallState.SIGNALING)
        page = self._page

        log.info("call: waiting for incoming call")

        # Navigate to messages to ensure long-poll is active
        await page.goto("https://vk.com/im")
        await page.wait_for_load_state("networkidle")

        # Wait for incoming call notification and answer it
        # VK shows a call overlay with an "Answer" button
        answer_button = page.locator(
            'button[data-testid="answer-call"], '
            '.vkuiButton--mode-primary:has-text("Ответить"), '
            'button:has-text("Answer"), '
            'button:has-text("Ответить")'
        )

        try:
            await answer_button.first.click(timeout=300_000)  # Wait up to 5 minutes
            log.info("call: answered incoming call")
        except Exception as e:
            log.error("call: failed to answer", error=str(e))
            raise

        await self._wait_for_connection()

    async def _wait_for_connection(self) -> None:
        """Wait for WebRTC connection to be established."""
        page = self._page

        for _ in range(60):  # Wait up to 30 seconds
            ready = await page.evaluate("window.__vkTunnel?.ready || false")
            if ready:
                log.info("call: webrtc connected")
                await self._set_state(CallState.CONNECTED)
                self._call_started_at = asyncio.get_event_loop().time()

                # Start polling for incoming audio
                self._poll_task = asyncio.create_task(self._poll_incoming())

                # Start call rotation timer
                await self._start_rotation_timer()

                if self.on_link_ready:
                    await self.on_link_ready()
                return
            await asyncio.sleep(0.5)

        raise ConnectionError("WebRTC connection timeout")

    async def send_audio_frame(self, opus_packet: bytes) -> None:
        """
        Send audio data through the call.

        For Mode A (direct Opus): we actually send PCM samples that get
        encoded to Opus by the browser's WebRTC stack. For true direct
        Opus replacement, we'd need to bypass the browser's encoder —
        which requires the aiortc native approach.

        For the browser bridge, we encode data into PCM (FSK or direct
        sample mapping) and let the browser handle Opus encoding.
        """
        if not self._page:
            return

        # Convert bytes to base64 for JS bridge
        b64_data = base64.b64encode(opus_packet).decode("ascii")

        await self._page.evaluate(f"""
            window.__vkTunnel?.outgoingQueue.push('{b64_data}');
        """)

    async def _poll_incoming(self) -> None:
        """Poll for incoming audio frames from the browser."""
        while self._state == CallState.CONNECTED:
            try:
                frames = await self._page.evaluate("""
                    (() => {{
                        const q = window.__vkTunnel?.incomingQueue || [];
                        const frames = q.splice(0, q.length);
                        return frames;
                    }})()
                """)

                for b64_frame in frames:
                    if self.on_audio_frame:
                        raw = base64.b64decode(b64_frame)
                        await self.on_audio_frame(raw)

            except Exception as e:
                log.warning("poll error", error=str(e))

            await asyncio.sleep(0.015)  # ~66 polls/sec (faster than 50 fps)

    async def end_call(self) -> None:
        """Hang up the current call."""
        if self._poll_task:
            self._poll_task.cancel()
            try:
                await self._poll_task
            except asyncio.CancelledError:
                pass

        if self._page:
            try:
                # Click the hang-up button
                await self._page.evaluate("""
                    // Try to find and click hangup button
                    const btn = document.querySelector(
                        'button[data-testid="end-call"], ' +
                        '.vkuiButton--mode-destructive, ' +
                        'button:has-text("Завершить"), ' +
                        'button[aria-label*="завер"]'
                    );
                    if (btn) btn.click();

                    // Also close the PeerConnection directly
                    if (window.__vkTunnel?.pc) {
                        window.__vkTunnel.pc.close();
                        window.__vkTunnel.ready = false;
                    }
                """)
            except Exception:
                pass

        await self._set_state(CallState.IDLE)
        log.info("call: ended")

    async def close(self) -> None:
        """Full cleanup — close browser."""
        await self.stop()
        if self._browser:
            await self._browser.close()
        if self._playwright:
            await self._playwright.stop()
