"""Media player entity for HA aux.

Provides a Home Assistant media_player entity that proxies playback
commands to the HA aux add-on via its HTTP API.
"""

from __future__ import annotations

import logging
from typing import Any

from homeassistant.components.media_player import (
    BrowseMedia,
    MediaPlayerEntity,
    MediaPlayerEntityFeature,
    MediaPlayerState,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from . import HaAuxDataUpdateCoordinator
from .const import (
    CONF_API_URL,
    CONF_DEVICE_NAME,
    DEFAULT_API_URL,
    DEFAULT_DEVICE_NAME,
    DOMAIN,
    DOMAIN_NAME,
)

_LOGGER = logging.getLogger(__name__)

# Features supported by this renderer.
# Intentionally excludes: NEXT_TRACK, PREVIOUS_TRACK, REPEAT, SHUFFLE,
# PLAY_MEDIA is included so HA / Music Assistant / TTS can send audio.
SUPPORTED_FEATURES = (
    MediaPlayerEntityFeature.PLAY_MEDIA
    | MediaPlayerEntityFeature.PAUSE
    | MediaPlayerEntityFeature.STOP
    | MediaPlayerEntityFeature.PLAY
    | MediaPlayerEntityFeature.VOLUME_SET
    | MediaPlayerEntityFeature.VOLUME_MUTE
    | MediaPlayerEntityFeature.SEEK
)


async def async_setup_entry(
    hass: HomeAssistant,
    config_entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the HA aux media player platform from a config entry."""
    coordinator: HaAuxDataUpdateCoordinator = hass.data[DOMAIN][config_entry.entry_id]

    device_name = config_entry.data.get(CONF_DEVICE_NAME, DEFAULT_DEVICE_NAME)
    api_url = config_entry.data.get(CONF_API_URL, DEFAULT_API_URL)

    async_add_entities(
        [
            HaAuxMediaPlayer(
                coordinator=coordinator,
                device_name=device_name,
                api_url=api_url,
                entry_id=config_entry.entry_id,
            )
        ]
    )


class HaAuxMediaPlayer(
    CoordinatorEntity[HaAuxDataUpdateCoordinator], MediaPlayerEntity
):
    """Representation of the HA aux audio renderer.

    This is a pure renderer — it does not manage playlists, libraries,
    or queues. All media content is provided externally by Home Assistant
    or Music Assistant.
    """

    _attr_has_entity_name = True

    def __init__(
        self,
        coordinator: HaAuxDataUpdateCoordinator,
        device_name: str,
        api_url: str,
        entry_id: str,
    ) -> None:
        """Initialize the media player.

        Args:
            coordinator: DataUpdateCoordinator for periodic status polling.
            device_name: Human-readable name for the entity.
            api_url: Base URL of the HA aux add-on API.
            entry_id: Config entry ID for unique identification.
        """
        super().__init__(coordinator)
        self._api_url = api_url
        self._attr_unique_id = f"ha_aux_{entry_id}"
        self._attr_name = device_name
        self._attr_supported_features = SUPPORTED_FEATURES

        # Device info for the integration registry
        sw_version = "1.0.0"
        if coordinator.data and "version" in coordinator.data:
            sw_version = coordinator.data["version"]

        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry_id)},
            name=device_name,
            manufacturer="HA aux",
            model="Audio Renderer",
            sw_version=sw_version,
        )

    # ------------------------------------------------------------------
    # State mapping
    # ------------------------------------------------------------------

    @property
    def state(self) -> MediaPlayerState | None:
        """Return the current media player state."""
        if self.coordinator.data is None:
            return MediaPlayerState.OFF

        mpv_state = self.coordinator.data.get("state", "idle")

        state_map: dict[str, MediaPlayerState] = {
            "playing": MediaPlayerState.PLAYING,
            "paused": MediaPlayerState.PAUSED,
            "idle": MediaPlayerState.IDLE,
            "stopped": MediaPlayerState.IDLE,
        }
        return state_map.get(mpv_state, MediaPlayerState.IDLE)

    @property
    def available(self) -> bool:
        """Return True if the add-on is reachable."""
        return self.coordinator.last_update_success

    # ------------------------------------------------------------------
    # Volume
    # ------------------------------------------------------------------

    @property
    def volume_level(self) -> float | None:
        """Return volume as a float between 0.0 and 1.0."""
        if self.coordinator.data is None:
            return None
        volume = self.coordinator.data.get("volume", 0)
        return max(0.0, min(1.0, volume / 100.0))

    @property
    def is_volume_muted(self) -> bool | None:
        """Return True if muted."""
        if self.coordinator.data is None:
            return None
        return bool(self.coordinator.data.get("muted", False))

    # ------------------------------------------------------------------
    # Media info
    # ------------------------------------------------------------------

    @property
    def media_position(self) -> float | None:
        """Return current playback position in seconds."""
        if self.coordinator.data is None:
            return None
        return self.coordinator.data.get("media_position")

    @property
    def media_duration(self) -> float | None:
        """Return total media duration in seconds."""
        if self.coordinator.data is None:
            return None
        return self.coordinator.data.get("media_duration")

    @property
    def media_title(self) -> str | None:
        """Return the title of the currently playing media."""
        if self.coordinator.data is None:
            return None
        return self.coordinator.data.get("media_title")

    @property
    def source(self) -> str | None:
        """Return the ALSA audio device identifier currently in use."""
        if self.coordinator.data is None:
            return None
        return self.coordinator.data.get("audio_device")

    # ------------------------------------------------------------------
    # Playback control
    # ------------------------------------------------------------------

    async def async_play_media(
        self, media_type: str, media_id: str, **kwargs: Any
    ) -> None:
        """Play media from a URL.

        Called by Home Assistant core, Music Assistant, TTS, Assist, etc.
        The media_id is the URL to play. It is passed directly to MPV.
        """
        _LOGGER.debug(
            "play_media: type=%s, id=%s, kwargs=%s", media_type, media_id, kwargs
        )

        title = media_id.rsplit("/", 1)[-1] if "/" in media_id else media_id

        data = {"url": media_id, "title": title}

        try:
            await self.coordinator.async_send_command("play", data)
            await self.coordinator.async_request_refresh()
        except Exception as err:
            _LOGGER.error("Failed to play media: %s", err)

    async def async_media_pause(self) -> None:
        """Pause playback."""
        try:
            await self.coordinator.async_send_command("pause")
            await self.coordinator.async_request_refresh()
        except Exception as err:
            _LOGGER.error("Failed to pause: %s", err)

    async def async_media_play(self) -> None:
        """Resume playback."""
        try:
            await self.coordinator.async_send_command("resume")
            await self.coordinator.async_request_refresh()
        except Exception as err:
            _LOGGER.error("Failed to resume: %s", err)

    async def async_media_stop(self) -> None:
        """Stop playback."""
        try:
            await self.coordinator.async_send_command("stop")
            await self.coordinator.async_request_refresh()
        except Exception as err:
            _LOGGER.error("Failed to stop: %s", err)

    async def async_set_volume_level(self, volume: float) -> None:
        """Set volume (0.0 to 1.0)."""
        vol = max(0, min(100, int(volume * 100)))
        try:
            await self.coordinator.async_send_command("volume", {"volume": vol})
            await self.coordinator.async_request_refresh()
        except Exception as err:
            _LOGGER.error("Failed to set volume: %s", err)

    async def async_mute_volume(self, mute: bool) -> None:
        """Mute or unmute."""
        try:
            await self.coordinator.async_send_command("mute", {"mute": mute})
            await self.coordinator.async_request_refresh()
        except Exception as err:
            _LOGGER.error("Failed to set mute: %s", err)

    async def async_media_seek(self, position: float) -> None:
        """Seek to a position in seconds."""
        try:
            await self.coordinator.async_send_command("seek", {"position": position})
            await self.coordinator.async_request_refresh()
        except Exception as err:
            _LOGGER.error("Failed to seek: %s", err)

    async def async_browse_media(
        self,
        media_content_type: str | None = None,
        media_content_id: str | None = None,
    ) -> BrowseMedia:
        """Return a dummy browse media result.

        We don't manage a media library, but this avoids errors when
        Home Assistant or Music Assistant calls browse_media.
        """
        return BrowseMedia(
            title=self._attr_name or "HA aux",
            media_class="directory",
            media_content_id="",
            media_content_type="",
            can_play=False,
            can_expand=False,
            children=[],
        )
