import asyncio
import logging
from homeassistant.components.lock import LockEntity
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .helpers import get_room_name, assign_device_area

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(hass, entry, async_add_entities):
    """Set up the Crestron Lock platform."""
    data = hass.data["crestron_home"][entry.entry_id]
    api, coordinator = data["api"], data["coordinator"]

    # 1. Fetch the data stored under 'locks' by __init__.py
    locks_data = (coordinator.data or {}).get("locks", {})

    if isinstance(locks_data, dict):
        # 2. The list inside is named 'doorLocks'
        device_list = locks_data.get("doorLocks", [])
        async_add_entities([
            CrestronLock(coordinator, api, dev["id"], dev["name"])
            for dev in device_list
        ])


class CrestronLock(CoordinatorEntity, LockEntity):
    """Representation of a Crestron Lock."""

    def __init__(self, coordinator, api, idx, name):
        super().__init__(coordinator)
        self._api = api
        self._id = idx
        self._attr_name = name
        self._attr_unique_id = f"crestron_lock_{idx}"

    def _get_room_id(self):
        locks = self.coordinator.data.get("locks", {}).get("doorLocks", [])
        for lock in locks:
            if str(lock.get("id")) == str(self._id):
                return lock.get("roomId")
        return None

    @property
    def device_info(self):
        """Link this lock to the processor device in HA, and suggest its Area."""
        info = {
            "identifiers": {("crestron_home", self._id)},
            "name": self._attr_name,
            "manufacturer": "Crestron",
            "model": "Smart Lock",
            "via_device": ("crestron_home", "processor"),
        }
        room_name = get_room_name(self.coordinator, self._get_room_id())
        if room_name:
            info["suggested_area"] = room_name
        return info

    async def async_added_to_hass(self):
        await super().async_added_to_hass()
        room_name = get_room_name(self.coordinator, self._get_room_id())
        assign_device_area(self.hass, {("crestron_home", self._id)}, room_name)

    @property
    def is_locked(self) -> bool:
        """Return True if the lock is locked."""
        # Find this specific lock in the doorLocks list
        locks = self.coordinator.data.get("locks", {}).get("doorLocks", [])
        for lock in locks:
            if str(lock.get("id")) == str(self._id):
                # This firmware reports 'Locked'/'Unlocked' (capitalized),
                # not the lowercase 'locked' the public docs show — compare
                # case-insensitively so this keeps working across firmware versions.
                return str(lock.get("status", "")).strip().lower() == "locked"
        return False

    @property
    def extra_state_attributes(self):
        """Show the Crestron Room name in attributes for easy mapping."""
        locks = self.coordinator.data.get("locks", {}).get("doorLocks", [])
        room_name = "Unknown Room"
        for lock in locks:
            if str(lock.get("id")) == str(self._id):
                room_id = str(lock.get("roomId"))
                room_map = self.coordinator.data.get("room_map", {})
                room_name = room_map.get(room_id, f"Room {room_id}")
                break
        return {"crestron_room": room_name, "crestron_id": self._id}

    async def async_lock(self, **kwargs):
        """Command to lock the door."""
        await self._api.request("POST", f"/doorlocks/lock/{self._id}")
        await asyncio.sleep(0.5)
        await self.coordinator.async_request_refresh()

    async def async_unlock(self, **kwargs):
        """Command to unlock the door."""
        await self._api.request("POST", f"/doorlocks/unlock/{self._id}")
        await asyncio.sleep(0.5)
        await self.coordinator.async_request_refresh()
