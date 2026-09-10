import asyncio
from homeassistant.components.alarm_control_panel import (
    AlarmControlPanelEntity,
    AlarmControlPanelEntityFeature,
    AlarmControlPanelState
)
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .helpers import get_room_name, assign_device_area


async def async_setup_entry(hass, entry, async_add_entities):
    """
    Set up Crestron Security platform.

    NOTE: this file is UNVERIFIED. GET /securitydevices returned an empty
    list on every system this integration has been tested against so far,
    so none of the field names (currentState, "ArmAway"/"ArmHome") or the
    write endpoints (/securitydevices/armhome, armaway, disarm) have ever
    actually been confirmed against real data. Treat this the same way as
    every other untested guess in this project: verify with curl against a
    real security panel before trusting it, the same way locks/thermostats/
    the PDU were all confirmed before being relied on.
    """
    data = hass.data["crestron_home"][entry.entry_id]
    api, coordinator = data["api"], data["coordinator"]

    sec_data = (coordinator.data or {}).get("security", {})
    if isinstance(sec_data, dict):
        device_list = sec_data.get("securityDevices", [])
        async_add_entities([CrestronAlarm(coordinator, api, dev["id"], dev["name"]) for dev in device_list])


class CrestronAlarm(CoordinatorEntity, AlarmControlPanelEntity):
    """Representation of a Crestron Security Device."""

    def __init__(self, coordinator, api, idx, name):
        super().__init__(coordinator)
        self._api, self._id, self._attr_name = api, idx, name
        self._attr_unique_id = f"crestron_alarm_{idx}"
        self._attr_supported_features = (
            AlarmControlPanelEntityFeature.ARM_HOME | AlarmControlPanelEntityFeature.ARM_AWAY
        )

    def _get_alarm_data(self):
        systems = self.coordinator.data.get("security", {}).get("securityDevices", [])
        for sys in systems:
            if str(sys.get("id")) == str(self._id):
                return sys
        return {}

    async def async_added_to_hass(self):
        await super().async_added_to_hass()
        room_name = get_room_name(self.coordinator, self._get_alarm_data().get("roomId"))
        assign_device_area(self.hass, {("crestron_home", self._id)}, room_name)

    @property
    def device_info(self):
        info = {
            "identifiers": {("crestron_home", self._id)},
            "name": self._attr_name,
            "manufacturer": "Crestron",
            "model": "Security Panel",
        }
        room_name = get_room_name(self.coordinator, self._get_alarm_data().get("roomId"))
        if room_name:
            info["suggested_area"] = room_name
        return info

    @property
    def alarm_state(self):
        data = self._get_alarm_data()
        if not data:
            return None
        status = data.get("currentState")
        if status == "ArmAway":
            return AlarmControlPanelState.ARMED_AWAY
        if status == "ArmHome":
            return AlarmControlPanelState.ARMED_HOME
        return AlarmControlPanelState.DISARMED

    @property
    def extra_state_attributes(self):
        data = self._get_alarm_data()
        room_name = get_room_name(self.coordinator, data.get("roomId")) or "Unknown Room"
        return {"crestron_room": room_name}

    async def async_alarm_arm_home(self, code=None):
        await self._api.request("POST", f"/securitydevices/armhome/{self._id}", {"code": code})
        await asyncio.sleep(1.0)  # Alarms take longer to commit
        await self.coordinator.async_request_refresh()

    async def async_alarm_arm_away(self, code=None):
        await self._api.request("POST", f"/securitydevices/armaway/{self._id}", {"code": code})
        await asyncio.sleep(1.0)
        await self.coordinator.async_request_refresh()

    async def async_alarm_disarm(self, code=None):
        await self._api.request("POST", f"/securitydevices/disarm/{self._id}", {"code": code})
        await asyncio.sleep(1.0)
        await self.coordinator.async_request_refresh()
