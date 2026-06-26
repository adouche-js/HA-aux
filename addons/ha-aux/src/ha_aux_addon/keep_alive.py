"""Keep-alive audio signal for HA aux.

Generates periodic, very-low-volume audio signals to prevent powered speakers
from entering standby mode when the player is idle.

This feature is optional and disabled by default. Effectiveness depends on
the specific speaker hardware.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

_LOGGER = logging.getLogger(__name__)


class KeepAlive:
    """Periodically emits low-level audio to prevent speaker standby.

    The signal is only emitted when the player has been idle (no playback)
    for longer than the configured interval. Any playback activity resets
    the idle timer.
    """

    def __init__(self, config: dict[str, Any], audio_device: str) -> None:
        """Initialize keep-alive from add-on configuration.

        Args:
            config: Add-on configuration dictionary.
            audio_device: ALSA device identifier (e.g. "hw:0,0").
        """
        self._enabled = bool(config.get("keep_alive_enabled", False))
        self._interval = int(config.get("keep_alive_interval", 60))
        self._duration_ms = int(config.get("keep_alive_duration", 100))
        self._signal_type = str(config.get("keep_alive_type", "sine"))
        self._frequency = int(config.get("keep_alive_frequency", 60))
        self._volume = float(config.get("keep_alive_volume", 0.01))
        self._audio_device = audio_device

        self._task: asyncio.Task[None] | None = None
        self._running = False
        self._last_activity: float = time.monotonic()
        self._player_active = False

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    @property
    def enabled(self) -> bool:
        """Whether keep-alive is enabled in configuration."""
        return self._enabled

    def notify_activity(self) -> None:
        """Notify that playback activity just occurred.

        Resets the idle timer so the keep-alive signal is not emitted
        when media is being played.
        """
        self._last_activity = time.monotonic()

    def set_player_active(self, active: bool) -> None:
        """Set the player active state.

        Args:
            active: True when the player is playing or paused,
                    False when idle/stopped.
        """
        self._player_active = active
        if active:
            self.notify_activity()

    async def start(self) -> None:
        """Start the keep-alive loop if enabled in config."""
        if not self._enabled:
            _LOGGER.debug("Keep-alive feature is disabled")
            return

        _LOGGER.info(
            "Keep-alive started (interval=%ds, type=%s, freq=%dHz, "
            "duration=%dms, volume=%.4f)",
            self._interval,
            self._signal_type,
            self._frequency,
            self._duration_ms,
            self._volume,
        )
        self._running = True
        self._task = asyncio.create_task(self._run())

    async def stop(self) -> None:
        """Stop the keep-alive loop."""
        self._running = False
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None
        _LOGGER.debug("Keep-alive stopped")

    # ------------------------------------------------------------------
    # Internal loop
    # ------------------------------------------------------------------

    async def _run(self) -> None:
        """Main keep-alive loop."""
        _LOGGER.debug("Keep-alive loop started (interval=%ds)", self._interval)

        while self._running:
            try:
                await asyncio.sleep(self._interval)

                # Skip if actively playing
                if self._player_active:
                    _LOGGER.debug("Keep-alive: player active, skipping signal")
                    continue

                # Only emit if idle for at least one interval
                idle_time = time.monotonic() - self._last_activity
                if idle_time < self._interval:
                    _LOGGER.debug(
                        "Keep-alive: only idle for %.1fs, skipping",
                        idle_time,
                    )
                    continue

                await self._emit_signal()

            except asyncio.CancelledError:
                break
            except Exception as err:
                _LOGGER.error("Keep-alive loop error: %s", err)

        _LOGGER.debug("Keep-alive loop ended")

    # ------------------------------------------------------------------
    # Signal emission
    # ------------------------------------------------------------------

    async def _emit_signal(self) -> None:
        """Generate and play a low-level audio signal via ffmpeg + aplay.

        Uses ffmpeg to synthesize audio and pipes it to aplay for playback.
        """
        duration_sec = self._duration_ms / 1000.0

        if self._signal_type == "sine":
            ffmpeg_args = [
                "ffmpeg",
                "-f",
                "lavfi",
                "-i",
                f"sine=frequency={self._frequency}:duration={duration_sec}",
                "-af",
                f"volume={self._volume}",
                "-f",
                "wav",
                "-",
            ]
        elif self._signal_type == "white_noise":
            ffmpeg_args = [
                "ffmpeg",
                "-f",
                "lavfi",
                "-i",
                f"anoisesrc=d={duration_sec}:c=white:a={self._volume}",
                "-f",
                "wav",
                "-",
            ]
        else:
            _LOGGER.warning("Unknown keep-alive signal type: %s", self._signal_type)
            return

        _LOGGER.debug(
            "Emitting keep-alive signal (%s, %dms, vol=%.4f)",
            self._signal_type,
            self._duration_ms,
            self._volume,
        )

        try:
            ffmpeg_proc = await asyncio.create_subprocess_exec(
                *ffmpeg_args,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
            )

            aplay_proc = await asyncio.create_subprocess_exec(
                "aplay",
                "-D",
                self._audio_device,
                "-t",
                "wav",
                "-f",
                "cd",
                stdin=ffmpeg_proc.stdout,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )

            # Wait for both processes to complete
            await asyncio.gather(
                ffmpeg_proc.wait(),
                aplay_proc.wait(),
            )
        except FileNotFoundError as err:
            _LOGGER.error("Required tool not found for keep-alive signal: %s", err)
        except Exception as err:
            _LOGGER.warning("Keep-alive signal playback failed: %s", err)
