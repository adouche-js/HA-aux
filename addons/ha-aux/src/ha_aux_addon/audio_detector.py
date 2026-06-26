"""ALSA audio device detection for HA aux.

Detects available audio devices on the system and selects the best
output based on a priority system (jack > HDMI > USB > internal > unknown).
"""

from __future__ import annotations

import logging
import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

_LOGGER = logging.getLogger(__name__)

PROC_ASOUND_CARDS = Path("/proc/asound/cards")
PROC_ASOUND_PCM = Path("/proc/asound/pcm")


@dataclass
class AudioDevice:
    """Represents a detected ALSA audio device."""

    card_id: int
    device_id: int
    name: str
    description: str
    device_type: str  # "jack", "hdmi", "usb", "internal", "unknown"

    @property
    def hw_id(self) -> str:
        """Return the ALSA hardware ID string (e.g. 'hw:0,0')."""
        return f"hw:{self.card_id},{self.device_id}"

    def to_dict(self) -> dict[str, Any]:
        """Return dictionary representation."""
        return {
            "hw_id": self.hw_id,
            "name": self.name,
            "description": self.description,
            "type": self.device_type,
        }

    def __repr__(self) -> str:
        return (
            f"AudioDevice(hw_id={self.hw_id!r}, name={self.name!r}, "
            f"type={self.device_type!r})"
        )


DEVICE_PRIORITY: dict[str, int] = {
    "jack": 0,
    "hdmi": 1,
    "usb": 2,
    "internal": 3,
    "unknown": 4,
}


class AudioDetector:
    """Detects and selects ALSA audio devices on the system."""

    def __init__(self) -> None:
        self._devices: list[AudioDevice] = []

    def detect(self) -> list[AudioDevice]:
        """Detect all available ALSA audio devices.

        Returns a list of detected AudioDevice objects.
        """
        _LOGGER.info("Detecting ALSA audio devices...")
        self._devices = []

        # Try multiple methods to ensure detection
        self._detect_from_proc()
        self._detect_from_aplay()
        self._detect_from_dev_snd()

        # Remove duplicates (based on hw_id)
        seen_hw_ids = set()
        unique_devices = []
        for dev in self._devices:
            if dev.hw_id not in seen_hw_ids:
                unique_devices.append(dev)
                seen_hw_ids.add(dev.hw_id)
        self._devices = unique_devices

        if self._devices:
            _LOGGER.info("Found %d audio device(s):", len(self._devices))
            for dev in self._devices:
                _LOGGER.info("  %s - %s (%s)", dev.hw_id, dev.name, dev.device_type)
        else:
            _LOGGER.warning("No audio devices detected on this system")

        return self._devices

    # ------------------------------------------------------------------
    # Detection from /proc/asound (Linux kernel interface)
    # ------------------------------------------------------------------

    def _detect_from_proc(self) -> None:
        """Parse /proc/asound files to discover audio devices."""
        cards = self._parse_cards()
        if not cards:
            _LOGGER.debug("/proc/asound/cards returned no cards")
            return

        pcm_devices = self._parse_pcm()

        for card_id, card_name in cards.items():
            if card_id not in pcm_devices:
                _LOGGER.debug("Card %d (%s) has no PCM devices", card_id, card_name)
                continue

            for device_id, device_info in pcm_devices[card_id].items():
                dev_name = device_info.get("name", card_name)
                dev_desc = device_info.get("description", "")
                dev_type = self._classify_device(dev_name, dev_desc)

                device = AudioDevice(
                    card_id=card_id,
                    device_id=device_id,
                    name=dev_name,
                    description=dev_desc,
                    device_type=dev_type,
                )
                self._devices.append(device)

    def _parse_cards(self) -> dict[int, str]:
        """Parse /proc/asound/cards to get card name map.

        Returns dict mapping card_id -> card_name.
        """
        cards: dict[int, str] = {}
        if not PROC_ASOUND_CARDS.exists():
            _LOGGER.debug("File not found: %s", PROC_ASOUND_CARDS)
            return cards

        try:
            content = PROC_ASOUND_CARDS.read_text(encoding="utf-8")
        except OSError as err:
            _LOGGER.warning("Cannot read %s: %s", PROC_ASOUND_CARDS, err)
            return cards

        for line in content.splitlines():
            # Format: " 0 [PCH            ]: HDA-Intel - HDA Intel PCH"
            match = re.match(r"\s*(\d+)\s*\[(.+?)\]\s*:\s*(.+)", line)
            if match:
                card_id = int(match.group(1))
                card_name = match.group(2).strip()
                cards[card_id] = card_name

        _LOGGER.debug("Found %d sound card(s) in /proc/asound/cards", len(cards))
        return cards

    def _parse_pcm(self) -> dict[int, dict[int, dict[str, str]]]:
        """Parse /proc/asound/pcm to get PCM device info.

        Returns dict: card_id -> device_id -> {name, description}
        """
        devices: dict[int, dict[int, dict[str, str]]] = {}
        if not PROC_ASOUND_PCM.exists():
            _LOGGER.debug("File not found: %s", PROC_ASOUND_PCM)
            return devices

        try:
            content = PROC_ASOUND_PCM.read_text(encoding="utf-8")
        except OSError as err:
            _LOGGER.warning("Cannot read %s: %s", PROC_ASOUND_PCM, err)
            return devices

        for line in content.splitlines():
            # Format: "00-00: ALC283 Analog : ALC283 Analog : playback 1 : capture 1"
            match = re.match(r"(\d+)-(\d+):\s*\*?\s*(.*)", line)
            if match:
                card_id = int(match.group(1))
                device_id = int(match.group(2))
                description = match.group(3).strip()

                if card_id not in devices:
                    devices[card_id] = {}
                devices[card_id][device_id] = {
                    "name": description,
                    "description": description,
                }

        _LOGGER.debug(
            "Found PCM devices on %d card(s) in /proc/asound/pcm", len(devices)
        )
        return devices

    # ------------------------------------------------------------------
    # Fallback: use aplay -l
    # ------------------------------------------------------------------

    def _detect_from_aplay(self) -> None:
        """Fallback: run 'aplay -l' to list devices."""
        try:
            result = subprocess.run(
                ["aplay", "-l"],
                capture_output=True,
                text=True,
                timeout=10,
            )
        except FileNotFoundError:
            _LOGGER.warning("aplay not found, cannot detect audio devices")
            return
        except subprocess.TimeoutExpired:
            _LOGGER.warning("aplay -l timed out")
            return

        for line in result.stdout.splitlines():
            # Format: "card 0: PCH [HDA Intel PCH], device 0: ALC283 Analog [ALC283 Analog]"
            match = re.match(
                r"card\s+(\d+):\s+\S+\s+\[.*?\],\s+device\s+(\d+):\s+(.*?)\s*\[(.*?)\]",
                line,
            )
            if match:
                card_id = int(match.group(1))
                device_id = int(match.group(2))
                name = match.group(3).strip()
                desc = match.group(4).strip()
                dev_type = self._classify_device(name, desc)

                device = AudioDevice(
                    card_id=card_id,
                    device_id=device_id,
                    name=name,
                    description=desc,
                    device_type=dev_type,
                )
                self._devices.append(device)

    # ------------------------------------------------------------------
    # Detection from /dev/snd
    # ------------------------------------------------------------------

    def _detect_from_dev_snd(self) -> None:
        """Scan /dev/snd for PCM playback devices."""
        dev_snd = Path("/dev/snd")
        if not dev_snd.exists():
            _LOGGER.debug("/dev/snd does not exist")
            return

        # Look for pcmC0D0p, pcmC1D0p etc (p for playback)
        for pcm_path in dev_snd.glob("pcmC*D*p"):
            match = re.search(r"pcmC(\d+)D(\d+)p", pcm_path.name)
            if match:
                card_id = int(match.group(1))
                device_id = int(match.group(2))
                hw_id = f"hw:{card_id},{device_id}"

                # Skip if already detected
                if any(d.hw_id == hw_id for d in self._devices):
                    continue

                _LOGGER.debug("Found raw PCM device: %s", hw_id)
                self._devices.append(
                    AudioDevice(
                        card_id=card_id,
                        device_id=device_id,
                        name=f"Generic Audio Device ({hw_id})",
                        description=f"Raw ALSA device at {pcm_path}",
                        device_type="unknown",
                    )
                )

    # ------------------------------------------------------------------
    # Device classification
    # ------------------------------------------------------------------

    @staticmethod
    def _classify_device(name: str, description: str) -> str:
        """Classify the device type based on name and description keywords."""
        combined = (name + " " + description).lower()

        if any(
            kw in combined for kw in ("hdmi", "hdmi/", "display audio", "intel display")
        ):
            return "hdmi"
        if any(kw in combined for kw in ("usb", "usb audio", "usb-audio")):
            return "usb"
        if any(
            kw in combined
            for kw in (
                "headphone",
                "headset",
                "jack",
                "hp out",
                "line-out",
                "line out",
                "analog",
            )
        ):
            return "jack"
        if any(kw in combined for kw in ("speaker", "built-in", "internal", "beeper")):
            return "internal"

        return "unknown"

    # ------------------------------------------------------------------
    # Device selection
    # ------------------------------------------------------------------

    def select_best_device(self, preferred: str | None = None) -> AudioDevice | None:
        """Select the best available audio device.

        Priority: jack > hdmi > usb > internal > unknown.
        Within same type, lower card/device ID wins.

        Args:
            preferred: User-specified device (e.g. "hw:0,0") or "auto".

        Returns:
            The selected AudioDevice, or None if no devices available.
        """
        if not self._devices:
            self.detect()

        if not self._devices:
            _LOGGER.error("No audio devices available to select")
            return None

        # If user specified a device, try to find it
        if preferred and preferred != "auto":
            for dev in self._devices:
                if dev.hw_id == preferred:
                    _LOGGER.info(
                        "Using user-specified device: %s (%s)", dev.hw_id, dev.name
                    )
                    return dev
            _LOGGER.warning(
                "User-specified device '%s' not found. Falling back to auto-detection.",
                preferred,
            )

        # Sort by priority, then by card_id, then by device_id
        sorted_devices = sorted(
            self._devices,
            key=lambda d: (
                DEVICE_PRIORITY.get(d.device_type, 99),
                d.card_id,
                d.device_id,
            ),
        )

        best = sorted_devices[0]
        _LOGGER.info(
            "Auto-selected device: %s (%s - %s)",
            best.hw_id,
            best.name,
            best.device_type,
        )
        return best

    def get_device_info(self, hw_id: str) -> dict[str, Any] | None:
        """Get dict info for a specific device by hardware ID."""
        for dev in self._devices:
            if dev.hw_id == hw_id:
                return dev.to_dict()
        return None

    @property
    def devices(self) -> list[AudioDevice]:
        """Return the list of detected devices."""
        return list(self._devices)
