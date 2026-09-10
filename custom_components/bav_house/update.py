"""Update entities for the custom integrations, which Home Assistant has none for.

An add-on gets an Update button in the store for free. A custom integration gets
nothing - there is no store for it - so a house runs whatever was copied in at
commissioning until somebody notices. These entities close that: the same
repository the add-ons come from also publishes a version manifest, and each
managed integration appears in Settings > Updates like anything else.

Installing writes the new files and restarts Home Assistant, because a custom
integration is imported once at start and there is no other way to swap it.
"""
from __future__ import annotations

import logging

from homeassistant.components.update import UpdateEntity, UpdateEntityFeature
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.entity import EntityCategory
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import payload
from .const import DOMAIN
from .entity import house_device

_LOGGER = logging.getLogger(__name__)

PRETTY = {"crestron_home": "Crestron Home", "bav_house": "BAV House Setup"}


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry,
                            add_entities: AddEntitiesCallback) -> None:
    data = hass.data[DOMAIN][entry.entry_id]
    # update_before_add: without it these sit `unavailable` until the platform's
    # first scheduled poll, which is fifteen minutes of an installer looking at
    # an integration that appears not to know what it installed.
    add_entities(
        (BavIntegrationUpdate(hass, entry, data["coordinator"], domain)
         for domain in payload.MANAGED),
        update_before_add=True,
    )


class BavIntegrationUpdate(UpdateEntity):
    """One managed custom integration."""

    _attr_has_entity_name = True
    _attr_entity_category = EntityCategory.CONFIG
    _attr_supported_features = UpdateEntityFeature.INSTALL
    _attr_should_poll = True

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry, coordinator,
                 domain: str) -> None:
        self._hass = hass
        self._entry = entry
        self._coordinator = coordinator
        self._domain = domain
        self._installed: str | None = None
        self._attr_name = PRETTY.get(domain, domain)
        self._attr_unique_id = f"{entry.entry_id}_{domain}_update"
        self._attr_device_info = house_device(entry)

    @property
    def available(self) -> bool:
        # An integration that is not on this box is not "up to date", it is
        # absent - showing it as an update entity would invite installing
        # Crestron Home onto a house that has no processor.
        return self._installed is not None

    @property
    def installed_version(self) -> str | None:
        return self._installed

    @property
    def latest_version(self) -> str | None:
        entry = ((self._coordinator.data or {}).get("integrations")
                 or {}).get(self._domain) or {}
        # With no manifest reachable, claim no news rather than a false update.
        return entry.get("version") or self._installed

    @property
    def release_summary(self) -> str | None:
        entry = ((self._coordinator.data or {}).get("integrations")
                 or {}).get(self._domain) or {}
        return (entry.get("summary") or "")[:255] or None

    async def async_update(self) -> None:
        self._installed = await self._hass.async_add_executor_job(
            payload.installed_version, self._hass.config.config_dir, self._domain)

    async def async_install(self, version, backup, **kwargs) -> None:
        manifest = self._coordinator.data or {}
        if not manifest:
            manifest = await payload.fetch_manifest(
                async_get_clientsession(self._hass))
        installed = await payload.install(
            async_get_clientsession(self._hass), manifest, self._domain,
            self._hass.config.config_dir)
        self._installed = installed
        self.async_write_ha_state()
        _LOGGER.warning(
            "bav_house: %s updated to %s - restarting Home Assistant, which is "
            "the only way a custom integration is reloaded",
            self._domain, installed)
        await self._hass.services.async_call("homeassistant", "restart",
                                             blocking=False)
