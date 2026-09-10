import logging
from homeassistant.components.media_player import (
    MediaPlayerDeviceClass,
    MediaPlayerEntity,
    MediaPlayerEntityFeature,
    MediaPlayerState,
)
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .helpers import get_room_name, assign_device_area

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(hass, entry, async_add_entities):
    """Set up Crestron Media Rooms platform (read-only for now)."""
    data = hass.data["crestron_home"][entry.entry_id]
    api, coordinator = data["api"], data["coordinator"]

    media_data = (coordinator.data or {}).get("media_rooms", {})
    rooms = media_data.get("mediaRooms", []) if isinstance(media_data, dict) else []

    async_add_entities([
        CrestronMediaRoom(coordinator, api, room["id"], room["name"])
        for room in rooms
    ])


class CrestronMediaRoom(CoordinatorEntity, MediaPlayerEntity):
    """
    Read-only representation of a Crestron Media Room (AV Zone).

    NOTE: This entity intentionally has no controls (no power/volume/source
    buttons). We could not confirm a real write endpoint for media rooms -
    every guessed variant of POST /mediarooms/SetState and
    POST /mediarooms/state/{id} either returned a generic "invalid device id"
    error regardless of payload shape, or silently hit Crestron's catch-all
    root API response (same tell that exposed /quickactions/run as fake).
    Rather than ship buttons that look real but silently fail, this just
    displays accurate live status. Control can be added once the real
    endpoint is confirmed via a packet capture of the official Crestron
    Home app.
    """

    _attr_supported_features = MediaPlayerEntityFeature(0)  # No controls - status display only

    def __init__(self, coordinator, api, room_id, name):
        super().__init__(coordinator)
        self._api = api
        self._room_id = room_id
        self._attr_name = f"Media Room: {name}"
        self._attr_unique_id = f"crestron_mediaroom_{room_id}"
        self._attr_device_class = MediaPlayerDeviceClass.RECEIVER

    def _get_room_data(self):
        rooms = self.coordinator.data.get("media_rooms", {}).get("mediaRooms", [])
        for r in rooms:
            if str(r.get("id")) == str(self._room_id):
                return r
        return {}

    @property
    def device_info(self):
        info = {
            "identifiers": {("crestron_home", f"mediaroom_{self._room_id}")},
            "name": self._attr_name,
            "manufacturer": "Crestron",
            "model": "Media Room",
            "via_device": ("crestron_home", "processor"),
        }
        room_name = get_room_name(self.coordinator, self._get_room_data().get("roomId"))
        if room_name:
            info["suggested_area"] = room_name
        return info

    async def async_added_to_hass(self):
        await super().async_added_to_hass()
        room_name = get_room_name(self.coordinator, self._get_room_data().get("roomId"))
        assign_device_area(self.hass, {("crestron_home", f"mediaroom_{self._room_id}")}, room_name)

    @property
    def state(self) -> MediaPlayerState:
        data = self._get_room_data()
        power = str(data.get("currentPowerState", "")).strip().lower()
        return MediaPlayerState.ON if power == "on" else MediaPlayerState.OFF

    @property
    def volume_level(self):
        data = self._get_room_data()
        # Some rooms report availableVolumeControls: ["none"] - no real
        # volume control exists there, so currentVolumeLevel is meaningless.
        if "none" in [str(v).lower() for v in data.get("availableVolumeControls", [])]:
            return None
        vol = data.get("currentVolumeLevel", 0)
        return float(vol) / 100.0

    @property
    def is_volume_muted(self):
        data = self._get_room_data()
        return str(data.get("currentMuteState", "")).strip().lower() == "muted"

    @property
    def source(self):
        data = self._get_room_data()
        current_id = data.get("currentSourceId")
        for src in data.get("availableSources", []):
            if str(src.get("id")) == str(current_id):
                return src.get("sourceName")
        return None

    @property
    def source_list(self):
        data = self._get_room_data()
        return [s.get("sourceName") for s in data.get("availableSources", []) if "sourceName" in s]

    @property
    def extra_state_attributes(self):
        data = self._get_room_data()
        room_name = get_room_name(self.coordinator, data.get("roomId")) or "Unknown Room"
        return {"crestron_room": room_name}
