import logging

from homeassistant.components.switch import SwitchEntity

from .pdu_api import CrestronPduClient

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(hass, entry, async_add_entities):
    """
    Set up switch entities for every configured PDU.

    Reads shared, already-connected PDU clients from hass.data (set up
    once in __init__.py) instead of creating its own connection - this
    file only registers listeners on the existing client.
    """
    data = hass.data["crestron_home"][entry.entry_id]
    pdu_clients = data.get("pdu_clients", {})

    if not pdu_clients:
        _LOGGER.info("No PDUs configured, skipping PC-350 switch platform")
        return

    entities = []

    for pdu_host, pdu_info in pdu_clients.items():
        client: CrestronPduClient = pdu_info["client"]
        pdu_name = pdu_info["name"]
        full_state = pdu_info["initial_state"]

        outlets_data = full_state.get("Device", {}).get("PowerController", {}).get("Outlets", {})

        entity_by_id = {}
        for outlet_id, outlet_info in outlets_data.items():
            # Only create an on/off switch for outlets configured to allow
            # full control. Outlets set to "Power Cycle Only" report
            # IsFullControlEnabled: false.
            if not outlet_info.get("IsFullControlEnabled", False):
                continue

            entity = CrestronPduOutlet(
                client, pdu_host, pdu_name, outlet_id, outlet_info.get("Name", f"Outlet {outlet_id}")
            )
            entity.set_state(outlet_info.get("IsOn", False))
            entity.set_available(client.is_connected)
            entities.append(entity)
            entity_by_id[outlet_id] = entity

        def make_push_handler(entity_map):
            def handle_push(device_partial: dict):
                outlets = device_partial.get("PowerController", {}).get("Outlets", {})
                for outlet_id, changes in outlets.items():
                    if "IsOn" in changes and outlet_id in entity_map:
                        entity_map[outlet_id].set_state(changes["IsOn"])
            return handle_push

        client.add_state_listener(make_push_handler(entity_by_id))

        def make_availability_handler(entity_map):
            def handle_availability(is_connected: bool):
                for entity in entity_map.values():
                    entity.set_available(is_connected)
            return handle_availability

        client.add_availability_listener(make_availability_handler(entity_by_id))

    async_add_entities(entities)


class CrestronPduOutlet(SwitchEntity):
    """A single outlet on a PDU, kept live via WebSocket push, with real availability."""

    _attr_should_poll = False  # state arrives via push, never poll

    def __init__(self, client: CrestronPduClient, pdu_host: str, pdu_name: str, outlet_id: str, name: str):
        self._client = client
        self._pdu_host = pdu_host
        self._pdu_name = pdu_name
        self._outlet_id = outlet_id
        self._attr_name = name
        self._attr_unique_id = f"crestron_pdu_{pdu_host}_outlet_{outlet_id}"
        self._is_on = False

    @property
    def device_info(self):
        return {
            "identifiers": {("crestron_home", f"pdu_{self._pdu_host}")},
            "name": f"Crestron PDU ({self._pdu_name})",
            "manufacturer": "Crestron",
            "model": "PC-350V Series",
        }

    @property
    def is_on(self) -> bool:
        return self._is_on

    @property
    def extra_state_attributes(self):
        return {"crestron_outlet_number": int(self._outlet_id)}

    def set_state(self, is_on: bool):
        """Called by the WebSocket push handler to update state without polling."""
        self._is_on = is_on
        if self.hass is not None:
            self.async_write_ha_state()

    def set_available(self, is_available: bool):
        """Called when the PDU's connection status changes."""
        self._attr_available = is_available
        if self.hass is not None:
            self.async_write_ha_state()

    async def async_turn_on(self, **kwargs):
        await self._client.async_set_outlet(self._outlet_id, True)

    async def async_turn_off(self, **kwargs):
        await self._client.async_set_outlet(self._outlet_id, False)
