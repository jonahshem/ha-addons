from homeassistant.helpers import area_registry as ar
from homeassistant.helpers import device_registry as dr


def get_room_name(coordinator, room_id):
    """
    Look up a human-readable Crestron room name from a roomId.
    Returns None if unknown, so callers can decide whether to pass
    it along (skip if None so HA doesn't create a junk 'None' area).
    """
    if room_id is None:
        return None
    room_map = (coordinator.data or {}).get("room_map", {})
    return room_map.get(str(room_id))


def assign_device_area(hass, identifiers, room_name):
    """
    Explicitly assign a device to a Home Assistant Area matching its
    Crestron room, creating the Area if it doesn't exist yet.

    NOTE: 'suggested_area' in device_info only applies the moment a device
    is first created - it does nothing for devices that already exist in
    the registry (which is every device in an integration that's been
    running for a while). This does the same thing Sonos does: explicitly
    look up/create the Area and assign it, but only if the device doesn't
    already have an area set (so we never override a manual choice).
    """
    if not room_name:
        return

    area_registry = ar.async_get(hass)
    area = area_registry.async_get_area_by_name(room_name)
    if area is None:
        area = area_registry.async_create(room_name)

    device_registry = dr.async_get(hass)
    device = device_registry.async_get_device(identifiers=identifiers)
    if device is not None and device.area_id is None:
        device_registry.async_update_device(device.id, area_id=area.id)
