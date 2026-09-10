import logging
from datetime import timedelta

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .const import DOMAIN, DEFAULT_POLLING_INTERVAL, PLATFORMS
from .api import CrestronHomeAPI
from .pdu_api import CrestronPduClient

_LOGGER = logging.getLogger(__name__)


async def _async_setup_pdu_clients(hass: HomeAssistant, entry: ConfigEntry) -> dict:
    """
    Create ONE CrestronPduClient per configured PDU, log in, connect its
    WebSocket, and fetch its initial state - all ONCE, here, before any
    platform is set up. switch.py, button.py, and binary_sensor.py all
    read from the returned dict instead of each independently creating
    their own client/login/connection for the same PDU (which is what
    they used to do - three separate sessions per PDU, wasteful and
    pointless since they're all talking to the same device).

    Returns a dict keyed by pdu_host:
        {host: {"client": CrestronPduClient, "name": str, "initial_state": dict}}
    """
    pdu_clients = {}

    for pdu_config in entry.options.get("pdus", []):
        pdu_host = pdu_config.get("host")
        pdu_name = pdu_config.get("name") or pdu_host
        if not pdu_host:
            continue

        client = CrestronPduClient(
            pdu_host, pdu_config.get("username"), pdu_config.get("password")
        )

        if not await client.async_start():
            _LOGGER.error(
                "Could not connect to PDU '%s' (%s) - its entities will be "
                "created but marked unavailable until it reconnects automatically",
                pdu_name, pdu_host,
            )

        try:
            initial_state = await client.async_get_full_state()
        except Exception as err:
            _LOGGER.error(
                "Could not fetch initial state from PDU '%s' (%s): %s - skipping this PDU",
                pdu_name, pdu_host, err,
            )
            await client.async_close()
            continue

        pdu_clients[pdu_host] = {
            "client": client,
            "name": pdu_name,
            "initial_state": initial_state,
        }

    return pdu_clients


def _prune_disabled_platform_entities(hass: HomeAssistant, entry: ConfigEntry, enabled_platforms):
    """
    Delete entities (and empty devices) that belong to a platform the user
    has unchecked in the integration's Options. Without this, unchecking a
    platform only stops it from being *set up again* - the old entities stay
    registered forever and show up as unavailable.
    """
    entity_registry = er.async_get(hass)
    device_registry = dr.async_get(hass)

    entity_entries = er.async_entries_for_config_entry(entity_registry, entry.entry_id)

    removed_count = 0
    for entity_entry in entity_entries:
        platform_domain = entity_entry.entity_id.split(".")[0]
        if platform_domain not in enabled_platforms:
            _LOGGER.info(
                "Removing entity %s - platform '%s' is not selected in Options",
                entity_entry.entity_id,
                platform_domain,
            )
            entity_registry.async_remove(entity_entry.entity_id)
            removed_count += 1

    if removed_count:
        _LOGGER.info("Removed %d orphaned entity(ies) for disabled platforms", removed_count)

    # Clean up any devices that no longer have any entities attached
    # (e.g. a lock's device entry, once its lock entity has been removed).
    # The processor device is deliberately excluded - it's a parent device
    # referenced via `via_device` by everything else, so it will always
    # show zero directly-attached entities, but it should never be deleted.
    device_entries = dr.async_entries_for_config_entry(device_registry, entry.entry_id)
    for device_entry in device_entries:
        if (DOMAIN, "processor") in device_entry.identifiers:
            continue
        remaining = er.async_entries_for_device(
            entity_registry, device_entry.id, include_disabled_entities=True
        )
        if not remaining:
            _LOGGER.info("Removing now-empty device: %s", device_entry.name)
            device_registry.async_remove_device(device_entry.id)


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up Crestron Home from a config entry."""
    enabled_platforms = entry.options.get("platforms", entry.data.get("platforms", PLATFORMS))
    polling_interval = entry.options.get("polling_interval", entry.data.get("polling_interval", DEFAULT_POLLING_INTERVAL))

    api = CrestronHomeAPI(entry.data["host"], entry.data["token"])

    if not await api.login():
        _LOGGER.error("Could not authenticate with Crestron Home. Check token.")
        return False

    # Register the processor itself as a real device. Every platform file
    # links its entities to this via `via_device: ("crestron_home",
    # "processor")`, but that only works if a device with that identifier
    # actually exists - otherwise it's a dangling reference, which newer
    # Home Assistant versions warn about (and will hard-fail on eventually).
    # This must happen BEFORE pruning below, since the processor device
    # never has entities directly attached to it (only via_device children
    # do) - if pruning ran first, it would delete this device every reload
    # for appearing "empty."
    device_registry = dr.async_get(hass)
    device_registry.async_get_or_create(
        config_entry_id=entry.entry_id,
        identifiers={(DOMAIN, "processor")},
        name=f"Crestron Home ({entry.data['host']})",
        manufacturer="Crestron",
        model="Crestron Home Processor",
    )

    # Remove any leftover entities/devices from platforms that are no longer selected
    _prune_disabled_platform_entities(hass, entry, enabled_platforms)

    async def async_update_data():
        """Fetch all data from the processor in one parallel cycle."""
        try:
            rooms_raw = await api.request("GET", "/rooms") or {}
            rooms_list = rooms_raw.get("rooms", [])
            room_map = {str(room["id"]): room["name"] for room in rooms_list}

            data = {
                "room_map": room_map,
                "lights": await api.request("GET", "/lights") or {},
                "shades": await api.request("GET", "/shades") or {},
                "thermostats": await api.request("GET", "/thermostats") or {},
                "locks": await api.request("GET", "/doorlocks") or {},
                "security": await api.request("GET", "/securitydevices") or {},
                "scenes": await api.request("GET", "/scenes") or {},
                "sensors": await api.request("GET", "/sensors") or {},
                "media_rooms": await api.request("GET", "/mediarooms") or {},
            }
            return data
        except Exception as err:
            raise UpdateFailed(f"Error communicating with Crestron Home API: {err}")

    coordinator = DataUpdateCoordinator(
        hass,
        _LOGGER,
        name="Crestron Home",
        update_interval=timedelta(seconds=polling_interval),
        update_method=async_update_data,
    )

    await coordinator.async_config_entry_first_refresh()

    # Set up all PDU clients ONCE here, before any platform runs, so
    # switch.py/button.py/binary_sensor.py can share the same connections
    # instead of each opening their own.
    pdu_clients = await _async_setup_pdu_clients(hass, entry)

    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = {
        "api": api,
        "coordinator": coordinator,
        "loaded_platforms": enabled_platforms,
        "pdu_clients": pdu_clients,
    }

    await hass.config_entries.async_forward_entry_setups(entry, enabled_platforms)

    entry.async_on_unload(entry.add_update_listener(update_listener))

    return True


async def update_listener(hass: HomeAssistant, entry: ConfigEntry):
    """Handle options update by reloading the integration."""
    _LOGGER.info("Crestron Home settings updated, reloading integration...")
    await hass.config_entries.async_reload(entry.entry_id)


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry cleanly."""
    domain_data = hass.data.get(DOMAIN, {}).get(entry.entry_id)

    if not domain_data:
        return True

    loaded_platforms = domain_data.get("loaded_platforms", PLATFORMS)
    unload_ok = await hass.config_entries.async_unload_platforms(entry, loaded_platforms)

    if unload_ok:
        api = domain_data.get("api")
        if api:
            await api.async_close()

        for pdu_info in domain_data.get("pdu_clients", {}).values():
            await pdu_info["client"].async_close()

        hass.data[DOMAIN].pop(entry.entry_id, None)

    return unload_ok
