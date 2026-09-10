"""What commissioning did, visible in Home Assistant rather than only in a log.

Commissioning runs in the background and can take minutes on a Pi, most of it
building add-on images. Somebody standing in a plant room needs to know whether
it is working, stuck, or finished, without SSH - so every line the provisioner
records is on this entity's attributes, and its state is the short answer.
"""
from __future__ import annotations

from homeassistant.components.sensor import SensorDeviceClass, SensorEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import EntityCategory
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.util import dt as dt_util

from .const import CONF_HOSTNAME, DOMAIN, STATE_STEPS
from .entity import house_device

# The last lines only: the full run is in the log, and an attribute that grows
# without bound is a recorder problem waiting to happen.
KEEP_LINES = 30

NOT_STARTED = "not started"
RUNNING = "running"
OK = "ok"
FAILED = "failed"


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry,
                            add_entities: AddEntitiesCallback) -> None:
    add_entities([CommissioningSensor(hass, entry)])


class CommissioningSensor(SensorEntity):
    _attr_has_entity_name = True
    _attr_name = "Commissioning"
    _attr_icon = "mdi:home-plus-outline"
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_should_poll = True
    # 🔴 `options` without the enum device class is a ValueError on every state
    # write, not a warning - and every value returned by `native_value` has to
    # be in this list or it is the same error again.
    _attr_device_class = SensorDeviceClass.ENUM
    _attr_options = [NOT_STARTED, RUNNING, OK, FAILED]

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        self._hass = hass
        self._entry = entry
        self._attr_unique_id = f"{entry.entry_id}_commissioning"
        self._attr_device_info = house_device(entry)

    @property
    def _progress(self):
        return (self._hass.data.get(DOMAIN, {})
                .get(self._entry.entry_id, {})
                .get("progress"))

    @property
    def native_value(self) -> str:
        progress = self._progress
        if progress is None or (not progress.running and not progress.finished):
            return NOT_STARTED
        if progress.running:
            return RUNNING
        return FAILED if progress.error else OK

    @property
    def extra_state_attributes(self) -> dict:
        progress = self._progress
        if progress is None:
            return {}
        return {
            "step": progress.step,
            "error": progress.error or None,
            "hostname": self._entry.data.get(CONF_HOSTNAME) or None,
            "steps_completed": list(self._entry.options.get(STATE_STEPS) or []),
            "finished": (dt_util.utc_from_timestamp(progress.finished).isoformat()
                         if progress.finished else None),
            "log": [
                f"{line['step']}: {line['state']}"
                + (f" - {line['detail']}" if line.get("detail") else "")
                for line in progress.lines[-KEEP_LINES:]
            ],
        }
