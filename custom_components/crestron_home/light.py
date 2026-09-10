import asyncio
import logging
from homeassistant.components.light import LightEntity, ColorMode
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .helpers import get_room_name, assign_device_area

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(hass, entry, async_add_entities):
    """Set up the Crestron Light platform."""
    data = hass.data["crestron_home"][entry.entry_id]
    api, coordinator = data["api"], data["coordinator"]

    lights_data = (coordinator.data or {}).get("lights", {})
    if isinstance(lights_data, dict):
        device_list = lights_data.get("lights", [])
        async_add_entities([
            CrestronLight(coordinator, api, dev["id"], dev["name"])
            for dev in device_list
        ])


class CrestronLight(CoordinatorEntity, LightEntity):
    """Representation of a Crestron Light."""

    def __init__(self, coordinator, api, idx, name):
        """Initialize the light."""
        super().__init__(coordinator)
        self._api = api
        self._id = idx
        self._attr_name = name
        self._attr_unique_id = f"crestron_light_{idx}"
        self._attr_color_mode = ColorMode.BRIGHTNESS
        self._attr_supported_color_modes = {ColorMode.BRIGHTNESS}

    def _get_light_data(self):
        lights = self.coordinator.data.get("lights", {}).get("lights", [])
        for light in lights:
            if str(light.get("id")) == str(self._id):
                return light
        return {}

    @property
    def device_info(self):
        """Return device information for the registry, with suggested Area."""
        info = {
            "identifiers": {("crestron_home", self._id)},
            "name": self._attr_name,
            "manufacturer": "Crestron",
            "model": "Crestron Home Light",
            "via_device": ("crestron_home", "processor"),
        }
        room_name = get_room_name(self.coordinator, self._get_light_data().get("roomId"))
        if room_name:
            info["suggested_area"] = room_name
        return info

    async def async_added_to_hass(self):
        await super().async_added_to_hass()
        room_name = get_room_name(self.coordinator, self._get_light_data().get("roomId"))
        assign_device_area(self.hass, {("crestron_home", self._id)}, room_name)

    @property
    def is_on(self) -> bool:
        """Return True if light is on."""
        return self._get_light_data().get("level", 0) > 0

    @property
    def brightness(self) -> int:
        """Return the brightness of this light between 0..255."""
        # Scale Crestron 0-65535 to HA 0-255
        return int((self._get_light_data().get("level", 0) / 65535) * 255)

    @property
    def extra_state_attributes(self):
        """Show the Crestron Room and ID in attributes."""
        data = self._get_light_data()
        room_name = get_room_name(self.coordinator, data.get("roomId")) or "Unknown Room"
        return {"crestron_room": room_name, "crestron_id": self._id}

    async def async_turn_on(self, **kwargs):
        """Turn the light on."""
        brightness = kwargs.get("brightness", 255)
        level = int((brightness / 255) * 65535)

        await self._api.request("POST", "/lights/SetState", {"lights": [{"id": self._id, "level": level}]})
        await asyncio.sleep(0.5)
        await self.coordinator.async_request_refresh()

    async def async_turn_off(self, **kwargs):
        """Turn the light off."""
        await self._api.request("POST", "/lights/SetState", {"lights": [{"id": self._id, "level": 0}]})
        await asyncio.sleep(0.5)
        await self.coordinator.async_request_refresh()
