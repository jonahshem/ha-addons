import asyncio
import logging

from .crestron_home_api import paths as _paths, raw_to_pct
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

    Controllable media room (AV zone): power, volume, mute and source select.
    """

    # Controllable since the real per-verb endpoints were confirmed live (2026-09-16,
    # 14 Malke): POST /mediarooms/{id}/{power/on|off, volume/{0-100}, mute, unmute,
    # selectsource/{sid}}. The old read-only note guessed /mediarooms/SetState, which the
    # processor rejects. No transport control (play/pause/next) - the REST API has none.
    _attr_supported_features = (
        MediaPlayerEntityFeature.TURN_ON
        | MediaPlayerEntityFeature.TURN_OFF
        | MediaPlayerEntityFeature.VOLUME_SET
        | MediaPlayerEntityFeature.VOLUME_MUTE
        | MediaPlayerEntityFeature.SELECT_SOURCE
    )

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
        # currentVolumeLevel is an unbounded integer (NAX internal, ~0-65535);
        # the SET endpoint takes 0-100%. Bound it to HA's 0-1 for the slider.
        return min(1.0, max(0.0, (raw_to_pct(data.get("currentVolumeLevel", 0)) or 0) / 100.0))

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

    # -- controls (real per-verb /mediarooms endpoints) -------------------------------

    def _source_id(self, source_name):
        for src in self._get_room_data().get("availableSources", []):
            if str(src.get("sourceName")) == str(source_name):
                return src.get("id", src.get("sourceId"))
        return None

    async def _do(self, spec):
        method, path, body = spec
        await self._api.request(method, path, body)
        await asyncio.sleep(0.5)
        await self.coordinator.async_request_refresh()

    async def async_turn_on(self, **kwargs):
        await self._do(_paths.mediaroom(self._room_id, "power", "on"))

    async def async_turn_off(self, **kwargs):
        await self._do(_paths.mediaroom(self._room_id, "power", "off"))

    async def async_set_volume_level(self, volume):
        pct = max(0, min(100, int(round(float(volume) * 100))))
        await self._do(_paths.mediaroom(self._room_id, "volume", pct))

    async def async_mute_volume(self, mute):
        await self._do(_paths.mediaroom(self._room_id, "mute" if mute else "unmute"))

    async def async_select_source(self, source):
        sid = self._source_id(source)
        if sid is None:
            _LOGGER.warning("Crestron media room %s has no source named %r", self._room_id, source)
            return
        await self._do(_paths.mediaroom(self._room_id, "selectsource", sid))
