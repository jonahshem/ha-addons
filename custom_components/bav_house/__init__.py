"""BAV House Setup - commission a house's Home Assistant in one form.

Two ways in, one code path:

* An imaged box has this integration on disk already and `bav_house:` in its
  configuration.yaml, so on first boot it puts a "set up this house" card in
  Devices & Services and nobody has to know where to click.
* A box set up by hand gets the BAV House Setup add-on, whose only job is to
  put this integration on disk and restart. From there it is the same.

What it then does is in `provision.py`. What it keeps doing afterwards is check
for new versions of the custom integrations it looks after, since Home
Assistant will not.
"""
from __future__ import annotations

import logging
from datetime import timedelta

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator

from . import payload, provision
from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)

PLATFORMS = [Platform.SENSOR, Platform.UPDATE]
CONFIG_SCHEMA = cv.empty_config_schema(DOMAIN)
VERSION_SCAN = timedelta(hours=12)
SERVICE_PROVISION = "provision"


async def async_setup(hass: HomeAssistant, config: dict) -> bool:
    """Offer to commission a house that has never been commissioned.

    This is the whole point of the line the image seeder adds to
    configuration.yaml: without it Home Assistant never loads an integration
    that has no config entry, and a freshly imaged box would sit there looking
    like a stock one.
    """
    already_asking = hass.config_entries.flow.async_progress_by_handler(DOMAIN)
    if DOMAIN in config and not hass.config_entries.async_entries(DOMAIN) \
            and not already_asking:
        hass.async_create_task(
            hass.config_entries.flow.async_init(
                DOMAIN, context={"source": "integration_discovery"}, data={})
        )
    return True


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    session = async_get_clientsession(hass)

    async def _versions() -> dict:
        try:
            return await payload.fetch_manifest(session)
        except payload.PayloadError as err:
            # Not fatal: a house with no internet still runs, it just cannot be
            # told about an update. Logged at debug so it is not noise.
            _LOGGER.debug("bav_house: version check: %s", err)
            return {}

    # `config_entry` passed rather than left to the ContextVar: Home Assistant
    # deprecated the implicit form and it stops working in 2026.8, and
    # `async_config_entry_first_refresh` below refuses a coordinator without one.
    coordinator: DataUpdateCoordinator[dict] = DataUpdateCoordinator(
        hass, _LOGGER, config_entry=entry, name="bav_house versions",
        update_interval=VERSION_SCAN, update_method=_versions,
    )
    progress = provision.Progress()
    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = {
        "progress": progress, "coordinator": coordinator,
    }
    await coordinator.async_config_entry_first_refresh()
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    # Commissioning runs in the background rather than blocking setup: it
    # installs add-ons, which on a Pi is minutes, and Home Assistant must not
    # be held up by it.
    entry.async_create_background_task(
        hass, provision.run(hass, entry, progress), "bav_house provision")

    async def _provision(call: ServiceCall) -> None:
        if progress.running:
            _LOGGER.warning("bav_house: commissioning is already running")
            return
        await provision.run(hass, entry, progress)

    hass.services.async_register(DOMAIN, SERVICE_PROVISION, _provision)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    unloaded = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unloaded:
        hass.data.get(DOMAIN, {}).pop(entry.entry_id, None)
        if not hass.data.get(DOMAIN):
            hass.services.async_remove(DOMAIN, SERVICE_PROVISION)
    return unloaded
