"""HA aux HTTP API server.

Main entry point for the add-on. Loads configuration, initializes
audio detection, MPV controller, keep-alive, and serves the HTTP API.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import signal
import sys
from pathlib import Path
from typing import Any

from aiohttp import web

from . import __version__
from .audio_detector import AudioDetector
from .keep_alive import KeepAlive
from .mpv_controller import MPVController, MPVControllerError

_LOGGER = logging.getLogger(__name__)

CONFIG_PATH = Path("/data/options.json")
API_PORT = int(os.environ.get("HA_AUX_PORT", "8292"))


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------


def load_config() -> dict[str, Any]:
    """Load add-on configuration from /data/options.json.

    Returns an empty dict if the file is missing or invalid.
    """
    try:
        config = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
        _LOGGER.info("Configuration loaded from %s", CONFIG_PATH)
        return config
    except FileNotFoundError:
        _LOGGER.warning("Configuration file %s not found, using defaults", CONFIG_PATH)
        return {}
    except json.JSONDecodeError as err:
        _LOGGER.error("Invalid configuration JSON: %s", err)
        return {}


def setup_logging(config: dict[str, Any]) -> None:
    """Configure the logging subsystem.

    Uses DEBUG level when 'debug' is true in config, otherwise INFO.
    """
    log_level = logging.DEBUG if config.get("debug", False) else logging.INFO

    logging.basicConfig(
        level=log_level,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
        handlers=[logging.StreamHandler(sys.stdout)],
    )

    # Quiet noisy libraries
    logging.getLogger("aiohttp.access").setLevel(logging.WARNING)
    logging.getLogger("asyncio").setLevel(logging.WARNING)


# ---------------------------------------------------------------------------
# Application
# ---------------------------------------------------------------------------


class HaAuxServer:
    """Main application that ties together MPV, audio detection, and the HTTP API."""

    def __init__(self, config: dict[str, Any]) -> None:
        self._config = config

        # Components
        self._mpv: MPVController | None = None
        self._keep_alive: KeepAlive | None = None
        self._app = web.Application()
        self._runner: web.AppRunner | None = None
        self._audio_detector = AudioDetector()

        # State
        self._audio_device_id: str = "default"
        self._device_name: str = config.get("device_name", "HA aux")
        self._max_volume: int = max(0, min(100, int(config.get("max_volume", 100))))
        self._initial_volume: int = max(
            0, min(100, int(config.get("initial_volume", 50)))
        )

        # Register HTTP routes
        self._setup_routes()

    # ------------------------------------------------------------------
    # HTTP routes
    # ------------------------------------------------------------------

    def _setup_routes(self) -> None:
        self._app.router.add_get("/health", self._handle_health)
        self._app.router.add_get("/status", self._handle_status)
        self._app.router.add_get("/device", self._handle_device)
        self._app.router.add_post("/play", self._handle_play)
        self._app.router.add_post("/pause", self._handle_pause)
        self._app.router.add_post("/resume", self._handle_resume)
        self._app.router.add_post("/stop", self._handle_stop)
        self._app.router.add_post("/volume", self._handle_volume)
        self._app.router.add_post("/mute", self._handle_mute)
        self._app.router.add_post("/seek", self._handle_seek)

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def start(self) -> None:
        """Initialize all components and start the HTTP server."""
        _LOGGER.info("=" * 56)
        _LOGGER.info("  HA aux Audio Renderer  v%s", __version__)
        _LOGGER.info("=" * 56)
        _LOGGER.info("Device name: %s", self._device_name)

        # Detect and select audio device
        devices = self._audio_detector.detect()
        preferred = self._config.get("audio_output", "auto")
        best_device = self._audio_detector.select_best_device(preferred)

        if best_device:
            self._audio_device_id = best_device.hw_id
            _LOGGER.info(
                "Using audio device: %s (%s)",
                self._audio_device_id,
                best_device.name,
            )
        else:
            _LOGGER.warning("No suitable audio device found; will use 'default'")
            self._audio_device_id = "default"

        # Start the MPV controller
        self._mpv = MPVController(self._audio_device_id)
        self._mpv.on_state_change = self._on_mpv_state_change

        try:
            await self._mpv.start()
        except MPVControllerError as err:
            _LOGGER.error("Failed to start MPV: %s", err)
            _LOGGER.warning(
                "Will retry on first play request. Check ALSA configuration."
            )
            self._mpv = None
        else:
            await self._mpv.set_volume(self._initial_volume)
            _LOGGER.info("Initial volume set to %d", self._initial_volume)

        # Start keep-alive
        self._keep_alive = KeepAlive(self._config, self._audio_device_id)
        if self._mpv:
            self._keep_alive.set_player_active(False)
        await self._keep_alive.start()

        if not self._keep_alive.enabled:
            _LOGGER.info("Keep-alive feature is disabled")

        # Start HTTP API
        self._runner = web.AppRunner(self._app)
        await self._runner.setup()
        site = web.TCPSite(self._runner, "0.0.0.0", API_PORT)
        await site.start()

        _LOGGER.info("API server listening on http://0.0.0.0:%d", API_PORT)
        _LOGGER.info("HA aux started successfully")

    async def stop(self) -> None:
        """Gracefully stop all components."""
        _LOGGER.info("Shutting down HA aux...")

        if self._keep_alive:
            await self._keep_alive.stop()

        if self._mpv:
            await self._mpv.stop()

        if self._runner:
            await self._runner.cleanup()

        _LOGGER.info("HA aux shut down complete")

    # ------------------------------------------------------------------
    # MPV callbacks
    # ------------------------------------------------------------------

    async def _on_mpv_state_change(self, state: str) -> None:
        """React to MPV state changes."""
        _LOGGER.debug("MPV state -> %s", state)
        if self._keep_alive:
            is_active = state in ("playing", "paused")
            self._keep_alive.set_player_active(is_active)
            if is_active:
                self._keep_alive.notify_activity()

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    async def _ensure_mpv(self) -> MPVController:
        """Return the MPV controller, restarting it if necessary."""
        if self._mpv is not None and self._mpv._running:
            return self._mpv

        _LOGGER.info("Starting MPV (on-demand)...")
        self._mpv = MPVController(self._audio_device_id)
        self._mpv.on_state_change = self._on_mpv_state_change

        try:
            await self._mpv.start()
            await self._mpv.set_volume(self._initial_volume)
        except MPVControllerError as err:
            _LOGGER.error("Failed to start MPV on demand: %s", err)
            self._mpv = None
            raise

        _LOGGER.info("MPV started on demand")
        return self._mpv

    @staticmethod
    def _json_error(message: str, status: int = 400) -> web.Response:
        """Return a JSON error response."""
        return web.json_response({"error": message}, status=status)

    @staticmethod
    def _json_ok(data: dict[str, Any] | None = None) -> web.Response:
        """Return a JSON success response."""
        return web.json_response(data or {"status": "ok"})

    # ------------------------------------------------------------------
    # API endpoints
    # ------------------------------------------------------------------

    async def _handle_health(self, request: web.Request) -> web.Response:
        """GET /health — simple health check."""
        mpv_running = self._mpv is not None and self._mpv._running
        return web.json_response(
            {
                "status": "ok",
                "version": __version__,
                "mpv_running": mpv_running,
                "audio_device": self._audio_device_id,
            }
        )

    async def _handle_status(self, request: web.Request) -> web.Response:
        """GET /status — current playback state."""
        status: dict[str, Any] = {
            "state": "idle",
            "volume": self._initial_volume,
            "muted": False,
            "media_position": 0.0,
            "media_duration": 0.0,
            "media_url": None,
            "media_title": None,
            "audio_device": self._audio_device_id,
            "keep_alive_enabled": self._keep_alive.enabled
            if self._keep_alive
            else False,
            "version": __version__,
        }

        if self._mpv and self._mpv._running:
            try:
                mpv_status = await self._mpv.get_status()
                status.update(mpv_status)
            except MPVControllerError:
                _LOGGER.debug("Could not get MPV status (will return defaults)")

        # Clamp volume to max
        if status.get("volume", 0) > self._max_volume:
            status["volume"] = self._max_volume

        return web.json_response(status)

    async def _handle_device(self, request: web.Request) -> web.Response:
        """GET /device — audio device information."""
        devices = self._audio_detector.detect()
        return web.json_response(
            {
                "current_device": self._audio_device_id,
                "available_devices": [d.to_dict() for d in devices],
                "selected": self._config.get("audio_output", "auto"),
            }
        )

    async def _handle_play(self, request: web.Request) -> web.Response:
        """POST /play — load and play a media URL.

        Body: {"url": "http://...", "title": "optional"}
        """
        try:
            data = await request.json()
        except json.JSONDecodeError:
            return self._json_error("Invalid JSON body")

        url = data.get("url", "").strip()
        if not url:
            return self._json_error("Missing 'url' field")

        title = data.get("title")

        try:
            mpv = await self._ensure_mpv()
            await mpv.play_media(url, title)
        except MPVControllerError as err:
            return self._json_error(str(err), status=500)

        if self._keep_alive:
            self._keep_alive.set_player_active(True)
            self._keep_alive.notify_activity()

        return self._json_ok({"status": "playing", "url": url})

    async def _handle_pause(self, request: web.Request) -> web.Response:
        """POST /pause — pause playback."""
        try:
            mpv = await self._ensure_mpv()
            await mpv.pause()
        except MPVControllerError as err:
            return self._json_error(str(err), status=500)
        return self._json_ok({"status": "paused"})

    async def _handle_resume(self, request: web.Request) -> web.Response:
        """POST /resume — resume playback."""
        try:
            mpv = await self._ensure_mpv()
            await mpv.resume()
        except MPVControllerError as err:
            return self._json_error(str(err), status=500)

        if self._keep_alive:
            self._keep_alive.set_player_active(True)
            self._keep_alive.notify_activity()

        return self._json_ok({"status": "playing"})

    async def _handle_stop(self, request: web.Request) -> web.Response:
        """POST /stop — stop playback."""
        try:
            mpv = await self._ensure_mpv()
            await mpv.stop_playback()
        except MPVControllerError as err:
            return self._json_error(str(err), status=500)

        if self._keep_alive:
            self._keep_alive.set_player_active(False)

        return self._json_ok({"status": "stopped"})

    async def _handle_volume(self, request: web.Request) -> web.Response:
        """POST /volume — set volume.

        Body: {"volume": 50} (0-100)
        """
        try:
            data = await request.json()
        except json.JSONDecodeError:
            return self._json_error("Invalid JSON body")

        volume = data.get("volume")
        if volume is None or not isinstance(volume, (int, float)):
            return self._json_error("Missing or invalid 'volume' (0-100)")

        volume = max(0, min(self._max_volume, int(volume)))

        try:
            mpv = await self._ensure_mpv()
            await mpv.set_volume(volume)
        except MPVControllerError as err:
            return self._json_error(str(err), status=500)

        return self._json_ok({"status": "ok", "volume": volume})

    async def _handle_mute(self, request: web.Request) -> web.Response:
        """POST /mute — mute/unmute.

        Body: {"mute": true} or {"mute": false}
        """
        try:
            data = await request.json()
        except json.JSONDecodeError:
            return self._json_error("Invalid JSON body")

        mute = data.get("mute")
        if mute is None or not isinstance(mute, bool):
            return self._json_error("Missing or invalid 'mute' (true/false)")

        try:
            mpv = await self._ensure_mpv()
            await mpv.set_mute(mute)
        except MPVControllerError as err:
            return self._json_error(str(err), status=500)

        return self._json_ok({"status": "ok", "muted": mute})

    async def _handle_seek(self, request: web.Request) -> web.Response:
        """POST /seek — seek to position.

        Body: {"position": 30.0} (seconds)
        """
        try:
            data = await request.json()
        except json.JSONDecodeError:
            return self._json_error("Invalid JSON body")

        position = data.get("position")
        if position is None or not isinstance(position, (int, float)):
            return self._json_error("Missing or invalid 'position' (seconds)")

        position = max(0.0, float(position))

        try:
            mpv = await self._ensure_mpv()
            await mpv.seek(position)
        except MPVControllerError as err:
            return self._json_error(str(err), status=500)

        return self._json_ok({"status": "ok", "position": position})


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


async def main() -> None:
    """Application entry point.

    Loads config, sets up logging, starts the server, and
    waits for a shutdown signal.
    """
    config = load_config()
    setup_logging(config)

    server = HaAuxServer(config)
    loop = asyncio.get_running_loop()

    stop_event = asyncio.Event()

    def _signal_handler() -> None:
        """Handle shutdown signals."""
        _LOGGER.info("Received shutdown signal")
        stop_event.set()

    # Register signal handlers (Linux only)
    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(sig, _signal_handler)
        except (ValueError, OSError) as err:
            _LOGGER.debug("Could not register handler for %s: %s", sig, err)

    try:
        await server.start()
        await stop_event.wait()
    except asyncio.CancelledError:
        pass
    finally:
        await server.stop()


if __name__ == "__main__":
    asyncio.run(main())
