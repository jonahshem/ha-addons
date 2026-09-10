"""One device for the house, so the entities group under it rather than scatter."""
from __future__ import annotations

from homeassistant.config_entries import ConfigEntry
from homeassistant.helpers.device_registry import DeviceInfo

from .const import CONF_HOSTNAME, CONF_SITE_NAME, DOMAIN


def house_device(entry: ConfigEntry) -> DeviceInfo:
    return DeviceInfo(
        identifiers={(DOMAIN, entry.entry_id)},
        name=entry.data.get(CONF_SITE_NAME) or "This house",
        manufacturer="BAV",
        model="House commissioning",
        configuration_url=(
            f"https://{entry.data[CONF_HOSTNAME]}"
            if entry.data.get(CONF_HOSTNAME) else None
        ),
    )
