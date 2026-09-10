import asyncio
from homeassistant.components.cover import CoverEntity, CoverDeviceClass, CoverEntityFeature
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .helpers import get_room_name, assign_device_area


async def async_setup_entry(hass, entry, async_add_entities):
    """Set up Crestron Shade platform."""
    data = hass.data["crestron_home"][entry.entry_id]
    api, coordinator = data["api"], data["coordinator"]

    shades_data = (coordinator.data or {}).get("shades", {})
    if isinstance(shades_data, dict):
        device_list = shades_data.get("shades", [])
        async_add_entities([CrestronShade(coordinator, api, dev["id"], dev["name"]) for dev in device_list])


class CrestronShade(CoordinatorEntity, CoverEntity):
    """Representation of a Crestron Shade."""

    _attr_device_class = CoverDeviceClass.SHADE
    _attr_supported_features = (
        CoverEntityFeature.OPEN | CoverEntityFeature.CLOSE | CoverEntityFeature.SET_POSITION
    )

    def __init__(self, coordinator, api, idx, name):
        super().__init__(coordinator)
        self._api, self._id, self._attr_name = api, idx, name
        self._attr_unique_id = f"crestron_shade_{idx}"

    def _get_shade_data(self):
        shades = self.coordinator.data.get("shades", {}).get("shades", [])
        for shade in shades:
            if str(shade.get("id")) == str(self._id):
                return shade
        return {}

    @property
    def device_info(self):
        info = {
            "identifiers": {("crestron_home", self._id)},
            "name": self._attr_name,
            "manufacturer": "Crestron",
            "model": "Crestron Shade",
            "via_device": ("crestron_home", "processor"),
        }
        room_name = get_room_name(self.coordinator, self._get_shade_data().get("roomId"))
        if room_name:
            info["suggested_area"] = room_name
        return info

    async def async_added_to_hass(self):
        await super().async_added_to_hass()
        room_name = get_room_name(self.coordinator, self._get_shade_data().get("roomId"))
        assign_device_area(self.hass, {("crestron_home", self._id)}, room_name)

    @property
    def current_cover_position(self):
        return int((self._get_shade_data().get("position", 0) / 65535) * 100)

    @property
    def is_closed(self):
        return self.current_cover_position == 0

    @property
    def extra_state_attributes(self):
        data = self._get_shade_data()
        room_name = get_room_name(self.coordinator, data.get("roomId")) or "Unknown Room"
        return {"crestron_room": room_name}

    async def async_set_cover_position(self, **kwargs):
        pos = int((kwargs.get("position") / 100) * 65535)
        await self._api.request("POST", "/shades/SetState", {"shades": [{"id": self._id, "position": pos}]})
        await asyncio.sleep(0.5)
        await self.coordinator.async_request_refresh()

    async def async_open_cover(self, **kwargs):
        await self.async_set_cover_position(position=100)

    async def async_close_cover(self, **kwargs):
        await self.async_set_cover_position(position=0)
