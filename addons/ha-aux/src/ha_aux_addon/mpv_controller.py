"""MPV audio player controller for HA aux.

Manages an MPV process lifecycle and communicates via its JSON IPC interface
over a Unix socket. Provides async methods for playback control.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import signal
from pathlib import Path
from typing import Any, Callable, Coroutine

_LOGGER = logging.getLogger(__name__)

MPV_SOCKET_PATH = "/tmp/ha_aux_mpv.socket"

# Type alias for async callbacks
AsyncCallback = Callable[..., Coroutine[Any, Any, None]]


class MPVControllerError(Exception):
    """Raised when MPV control operations fail."""


class MPVController:
    """Controls an MPV process via its JSON IPC interface.

    Manages the MPV lifecycle, maintains state, and provides async methods
    for playback control. A single MPV process runs in idle mode and
    media is loaded on demand.
    """

    def __init__(self, audio_device: str = "default") -> None:
        """Initialize the MPV controller.

        Args:
            audio_device: ALSA audio device identifier (e.g. "hw:0,0").
        """
        self._audio_device = audio_device

        # MPV process
        self._process: asyncio.subprocess.Process | None = None
        self._reader: asyncio.StreamReader | None = None
        self._writer: asyncio.StreamWriter | None = None

        # Internal state
        self._running = False
        self._request_id = 0
        self._lock = asyncio.Lock()
        self._pending_requests: dict[int, asyncio.Future[dict[str, Any]]] = {}

        # Current playback state
        self.state: str = "idle"  # "playing", "paused", "idle"
        self.volume: int = 50
        self.muted: bool = False
        self.media_position: float = 0.0
        self.media_duration: float = 0.0
        self.media_url: str | None = None
        self.media_title: str | None = None

        # Optional async callbacks
        self.on_state_change: AsyncCallback | None = None
        self.on_position_change: AsyncCallback | None = None
        self.on_eof: AsyncCallback | None = None

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def start(self) -> None:
        """Start the MPV process and connect to its JSON IPC socket.

        Raises MPVControllerError if startup fails.
        """
        _LOGGER.info("Starting MPV with audio device: %s", self._audio_device)

        # Remove stale socket
        sock = Path(MPV_SOCKET_PATH)
        if sock.exists():
            _LOGGER.debug("Removing stale socket: %s", MPV_SOCKET_PATH)
            try:
                sock.unlink()
            except OSError as err:
                _LOGGER.warning("Could not remove stale socket: %s", err)

        # Build command
        cmd = [
            "mpv",
            "--idle",
            f"--input-ipc-server={MPV_SOCKET_PATH}",
            f"--ao=alsa",
            f"--audio-device=alsa/{self._audio_device}",
            "--no-terminal",
            "--no-video",
            "--no-keepaspect-window",
            "--no-input-default-bindings",
            "--no-osc",
            "--no-osd-bar",
            "--really-quiet",
        ]

        _LOGGER.debug("Starting MPV: %s", " ".join(cmd))

        self._process = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )

        # Wait for Unix socket to appear
        _LOGGER.debug("Waiting for MPV socket at %s", MPV_SOCKET_PATH)
        for attempt in range(50):
            if sock.exists():
                _LOGGER.debug("MPV socket ready after %dms", (attempt + 1) * 100)
                break
            await asyncio.sleep(0.1)
        else:
            # Check if process exited
            if self._process.returncode is not None:
                msg = (
                    f"MPV process exited prematurely with code "
                    f"{self._process.returncode}"
                )
                _LOGGER.error(msg)
                raise MPVControllerError(msg)
            msg = "MPV IPC socket did not appear within timeout"
            _LOGGER.error(msg)
            raise MPVControllerError(msg)

        # Connect to IPC
        try:
            self._reader, self._writer = await asyncio.open_unix_connection(
                MPV_SOCKET_PATH
            )
        except OSError as err:
            msg = f"Failed to connect to MPV socket: {err}"
            _LOGGER.error(msg)
            raise MPVControllerError(msg) from err

        self._running = True

        # Register property observers
        await self._observe_properties()

        # Start event listener task
        asyncio.create_task(self._event_listener())

        _LOGGER.info("MPV controller started successfully")

    async def stop(self) -> None:
        """Stop the MPV process and clean up resources."""
        _LOGGER.info("Stopping MPV controller")
        self._running = False

        # Close IPC connection
        if self._writer:
            try:
                self._writer.close()
                await self._writer.wait_closed()
            except Exception:
                pass
            self._writer = None
            self._reader = None

        # Terminate MPV process
        if self._process and self._process.returncode is None:
            _LOGGER.debug("Sending SIGTERM to MPV (pid %d)", self._process.pid)
            try:
                self._process.send_signal(signal.SIGTERM)
                try:
                    await asyncio.wait_for(self._process.wait(), timeout=5)
                except asyncio.TimeoutError:
                    _LOGGER.warning("MPV did not exit gracefully, sending SIGKILL")
                    self._process.kill()
                    await self._process.wait()
            except ProcessLookupError:
                _LOGGER.debug("MPV process already exited")

        # Clean up socket
        sock = Path(MPV_SOCKET_PATH)
        if sock.exists():
            try:
                sock.unlink()
            except OSError:
                pass

        _LOGGER.info("MPV controller stopped")

    async def restart(self) -> None:
        """Restart the MPV process."""
        _LOGGER.info("Restarting MPV controller")
        await self.stop()
        await self.start()

    # ------------------------------------------------------------------
    # IPC communication
    # ------------------------------------------------------------------

    async def _send_command(self, command: list[Any]) -> dict[str, Any]:
        """Send a JSON IPC command to MPV and return the response.

        Thread-safe via asyncio.Lock and asyncio.Future.
        """
        if not self._writer or self._writer.is_closing():
            raise MPVControllerError("Not connected to MPV")

        # Get a unique request ID
        async with self._lock:
            self._request_id += 1
            request_id = self._request_id

        # Create a future to wait for the response
        future: asyncio.Future[dict[str, Any]] = asyncio.Future()
        self._pending_requests[request_id] = future

        payload = json.dumps({"command": command, "request_id": request_id}) + "\n"

        try:
            self._writer.write(payload.encode("utf-8"))
            await self._writer.drain()
            return await asyncio.wait_for(future, timeout=30)
        except asyncio.TimeoutError as err:
            raise MPVControllerError(
                f"Timeout waiting for MPV response to {command[0]}"
            ) from err
        except (ConnectionError, OSError) as err:
            self._running = False
            raise MPVControllerError(f"MPV connection lost: {err}") from err
        finally:
            self._pending_requests.pop(request_id, None)

    async def _observe_properties(self) -> None:
        """Register property observers for automatic state tracking."""
        properties = [
            "pause",
            "time-pos",
            "duration",
            "volume",
            "mute",
            "eof-reached",
            "path",
            "filename",
        ]
        for prop_id, prop_name in enumerate(properties, 1):
            try:
                await self._send_command(["observe_property", prop_id, prop_name])
            except MPVControllerError as err:
                _LOGGER.debug("Failed to observe property '%s': %s", prop_name, err)

    # ------------------------------------------------------------------
    # Event handling
    # ------------------------------------------------------------------

    async def _event_listener(self) -> None:
        """Background task that reads from MPV's IPC socket.

        Dispatches messages to either pending command futures or
        to the event handler.
        """
        while self._running and self._reader:
            try:
                line = await self._reader.readline()
            except (ConnectionError, OSError) as err:
                if self._running:
                    _LOGGER.error("MPV socket read error: %s", err)
                break

            if not line:
                if self._running:
                    _LOGGER.debug("MPV IPC socket closed")
                break

            try:
                data = json.loads(line.decode("utf-8"))
            except json.JSONDecodeError:
                _LOGGER.debug("Invalid JSON from MPV: %s", line[:200])
                continue

            # Case 1: This is a response to a command
            request_id = data.get("request_id")
            if request_id is not None:
                future = self._pending_requests.get(request_id)
                if future and not future.done():
                    future.set_result(data)
                continue

            # Case 2: This is an event
            if "event" in data:
                await self._handle_event(data)

    async def _handle_event(self, event: dict[str, Any]) -> None:
        """Process a single MPV event."""
        event_type = event.get("event")

        if event_type == "property-change":
            await self._handle_property_change(event.get("name"), event.get("data"))

        elif event_type == "start-file":
            _LOGGER.debug("MPV: loading file")

        elif event_type == "file-loaded":
            _LOGGER.debug("MPV: file loaded")
            self.state = "playing"
            await self._fire_callback(self.on_state_change, "playing")

        elif event_type == "end-file":
            reason = event.get("reason", "unknown")
            _LOGGER.debug("MPV: file ended (%s)", reason)
            if reason in ("eof", "stop"):
                self.state = "idle"
                self.media_url = None
                self.media_title = None
                self.media_position = 0.0
                self.media_duration = 0.0
                await self._fire_callback(self.on_state_change, "idle")
                await self._fire_callback(self.on_eof)

        elif event_type == "shutdown":
            _LOGGER.info("MPV shutting down")
            self._running = False

    async def _handle_property_change(self, name: str | None, data: Any) -> None:
        """Update internal state from a property-change event."""
        if name == "pause":
            if data is True:
                self.state = "paused"
            elif data is False:
                self.state = "playing"
            await self._fire_callback(self.on_state_change, self.state)

        elif name == "time-pos":
            if isinstance(data, (int, float)):
                self.media_position = float(data)
                await self._fire_callback(self.on_position_change, self.media_position)

        elif name == "duration":
            if isinstance(data, (int, float)):
                self.media_duration = float(data)

        elif name == "volume":
            if isinstance(data, (int, float)):
                self.volume = int(data)

        elif name == "mute":
            self.muted = bool(data)

        elif name == "path":
            if isinstance(data, str):
                self.media_url = data

        elif name == "filename":
            if isinstance(data, str):
                self.media_title = data

    async def _fire_callback(self, callback: AsyncCallback | None, *args: Any) -> None:
        """Safely fire an async callback, catching exceptions."""
        if callback is None:
            return
        try:
            await callback(*args)
        except Exception as err:
            _LOGGER.error("Error in MPV callback: %s", err)

    # ------------------------------------------------------------------
    # Playback control
    # ------------------------------------------------------------------

    async def play_media(self, url: str, title: str | None = None) -> None:
        """Load and play a media URL.

        Args:
            url: URL or file path to play.
            title: Optional human-readable title.
        """
        _LOGGER.info("Loading media: %s", url)
        await self._send_command(["loadfile", url, "replace"])
        if title:
            self.media_title = title

    async def pause(self) -> None:
        """Pause playback."""
        _LOGGER.debug("Pausing playback")
        await self._send_command(["set_property", "pause", True])

    async def resume(self) -> None:
        """Resume playback."""
        _LOGGER.debug("Resuming playback")
        await self._send_command(["set_property", "pause", False])

    async def stop_playback(self) -> None:
        """Stop playback and unload the current file."""
        _LOGGER.debug("Stopping playback")
        await self._send_command(["stop"])

    async def set_volume(self, volume: int) -> None:
        """Set volume level (0-100)."""
        vol = max(0, min(100, volume))
        _LOGGER.debug("Setting volume to %d", vol)
        await self._send_command(["set_property", "volume", vol])

    async def set_mute(self, muted: bool) -> None:
        """Set mute state."""
        _LOGGER.debug("Setting mute to %s", muted)
        await self._send_command(["set_property", "mute", muted])

    async def seek(self, position: float) -> None:
        """Seek to an absolute position in seconds."""
        _LOGGER.debug("Seeking to %.1f seconds", position)
        await self._send_command(["seek", position, "absolute"])

    # ------------------------------------------------------------------
    # Status
    # ------------------------------------------------------------------

    async def get_property(self, name: str) -> Any:
        """Get a single MPV property value."""
        result = await self._send_command(["get_property", name])
        return result.get("data")

    async def get_status(self) -> dict[str, Any]:
        """Return a snapshot of the current playback status."""
        return {
            "state": self.state,
            "volume": self.volume,
            "muted": self.muted,
            "media_position": self.media_position,
            "media_duration": self.media_duration,
            "media_url": self.media_url,
            "media_title": self.media_title,
            "audio_device": self._audio_device,
        }
