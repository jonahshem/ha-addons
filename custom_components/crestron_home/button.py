import logging
from homeassistant.components.button import ButtonEntity
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .pdu_api import CrestronPduClient

_LOGGER = logging.getLogger(__name__)

# Scene "type" values that represent a relay, gate, or other momentary
# I/O trigger rather than a lighting/shade/lock/climate preset.
IO_SCENE_TYPES = {"genericio", "generic i/o", "i/o"}


async def async_setup_entry(hass, entry, async_add_entities):
    """Set up buttons for relay/gate/I-O scenes AND PDU outlet controls."""
    data = hass.data["crestron_home"][entry.entry_id]
    api, coordinator = data["api"], data["coordinator"]

    scenes_data = (coordinator.data or {}).get("scenes", {})
    scenes = scenes_data.get("scenes", []) if isinstance(scenes_data, dict) else []

    entities = []
    seen_ids = set()
    for s in scenes:
        scene_type = str(s.get("type", "")).strip().lower()
        scene_id = str(s.get("id"))
        if scene_type in IO_SCENE_TYPES and scene_id not in seen_ids:
            seen_ids.add(scene_id)
            entities.append(CrestronIoButton(coordinator, api, s["id"], s["name"]))

    # PDU buttons - uses the SAME shared, already-connected client from
    # __init__.py that switch.py and binary_sensor.py also use, instead
    # of opening its own separate connection.
    pdu_clients = data.get("pdu_clients", {})

    for pdu_host, pdu_info in pdu_clients.items():
        client: CrestronPduClient = pdu_info["client"]
        pdu_name = pdu_info["name"]
        full_state = pdu_info["initial_state"]

        pdu_buttons = []
        outlets_data = full_state.get("Device", {}).get("PowerController", {}).get("Outlets", {})
        for outlet_id, outlet_info in outlets_data.items():
            # Power Cycle is always available regardless of the outlet's
            # IsFullControlEnabled setting.
            button = CrestronPduCycleButton(
                client, pdu_host, pdu_name, outlet_id, outlet_info.get("Name", f"Outlet {outlet_id}")
            )
            button.set_available(client.is_connected)
            entities.append(button)
            pdu_buttons.append(button)

        # Device-level (not per-outlet) buttons
        reset_button = CrestronPduResetVoltageButton(client, pdu_host, pdu_name)
        reset_button.set_available(client.is_connected)
        entities.append(reset_button)
        pdu_buttons.append(reset_button)

        reboot_button = CrestronPduRebootButton(client, pdu_host, pdu_name)
        reboot_button.set_available(client.is_connected)
        entities.append(reboot_button)
        pdu_buttons.append(reboot_button)

        reset_reboot_button = CrestronPduResetAndRebootButton(client, pdu_host, pdu_name)
        reset_reboot_button.set_available(client.is_connected)
        entities.append(reset_reboot_button)
        pdu_buttons.append(reset_reboot_button)

        def make_availability_handler(buttons):
            def handle_availability(is_connected: bool):
                for b in buttons:
                    b.set_available(is_connected)
            return handle_availability

        client.add_availability_listener(make_availability_handler(pdu_buttons))

    async_add_entities(entities)


class CrestronIoButton(CoordinatorEntity, ButtonEntity):
    """A tappable button that triggers a relay/gate scene (e.g. 'Open Gate')."""

    def __init__(self, coordinator, api, scene_id, name):
        super().__init__(coordinator)
        self._api = api
        self._scene_id = scene_id
        self._attr_name = name
        self._attr_unique_id = f"crestron_io_button_{scene_id}"
        self._attr_icon = "mdi:electric-switch"

    @property
    def device_info(self):
        return {
            "identifiers": {("crestron_home", f"io_scene_{self._scene_id}")},
            "name": "Crestron Relays & Gates",
            "manufacturer": "Crestron",
            "model": "Relay / I-O Trigger",
            "via_device": ("crestron_home", "processor"),
        }

    async def async_press(self) -> None:
        """Trigger the relay/gate by recalling its underlying scene."""
        _LOGGER.info("Triggering Crestron relay/gate scene %s (%s)", self._scene_id, self.name)
        await self._api.request("POST", f"/scenes/recall/{self._scene_id}", {})


class CrestronPduCycleButton(ButtonEntity):
    """A tappable button that power cycles a single PDU outlet."""

    def __init__(self, client: CrestronPduClient, pdu_host: str, pdu_name: str, outlet_id: str, outlet_name: str):
        self._client = client
        self._outlet_id = outlet_id
        self._pdu_host = pdu_host
        self._pdu_name = pdu_name
        self._attr_name = f"{outlet_name} - Power Cycle"
        self._attr_unique_id = f"crestron_pdu_{pdu_host}_outlet_{outlet_id}_cycle"
        self._attr_icon = "mdi:restart"

    @property
    def device_info(self):
        return {
            "identifiers": {("crestron_home", f"pdu_{self._pdu_host}")},
            "name": f"Crestron PDU ({self._pdu_name})",
            "manufacturer": "Crestron",
            "model": "PC-350V Series",
        }

    @property
    def extra_state_attributes(self):
        return {"crestron_outlet_number": int(self._outlet_id)}

    def set_available(self, is_available: bool):
        self._attr_available = is_available
        if self.hass is not None:
            self.async_write_ha_state()

    async def async_press(self) -> None:
        _LOGGER.info("Power cycling PDU outlet %s on %s", self._outlet_id, self._pdu_host)
        await self._client.async_cycle_outlet(self._outlet_id)


class CrestronPduResetVoltageButton(ButtonEntity):
    """Device-level button that resets the PDU's voltage protection trip."""

    def __init__(self, client: CrestronPduClient, pdu_host: str, pdu_name: str):
        self._client = client
        self._pdu_host = pdu_host
        self._pdu_name = pdu_name
        self._attr_name = "Reset Voltage Protection"
        self._attr_unique_id = f"crestron_pdu_{pdu_host}_reset_voltage_protection"
        self._attr_icon = "mdi:flash-alert"

    @property
    def device_info(self):
        return {
            "identifiers": {("crestron_home", f"pdu_{self._pdu_host}")},
            "name": f"Crestron PDU ({self._pdu_name})",
            "manufacturer": "Crestron",
            "model": "PC-350V Series",
        }

    def set_available(self, is_available: bool):
        self._attr_available = is_available
        if self.hass is not None:
            self.async_write_ha_state()

    async def async_press(self) -> None:
        _LOGGER.info("Resetting voltage protection on PDU %s", self._pdu_host)
        await self._client.async_reset_voltage_protection()


class CrestronPduRebootButton(ButtonEntity):
    """Device-level button that reboots the entire PDU."""

    def __init__(self, client: CrestronPduClient, pdu_host: str, pdu_name: str):
        self._client = client
        self._pdu_host = pdu_host
        self._pdu_name = pdu_name
        self._attr_name = "Reboot PDU"
        self._attr_unique_id = f"crestron_pdu_{pdu_host}_reboot"
        self._attr_icon = "mdi:restart-alert"

    @property
    def device_info(self):
        return {
            "identifiers": {("crestron_home", f"pdu_{self._pdu_host}")},
            "name": f"Crestron PDU ({self._pdu_name})",
            "manufacturer": "Crestron",
            "model": "PC-350V Series",
        }

    def set_available(self, is_available: bool):
        self._attr_available = is_available
        if self.hass is not None:
            self.async_write_ha_state()

    async def async_press(self) -> None:
        _LOGGER.info("Rebooting PDU %s (device will be offline for ~3.5 minutes)", self._pdu_host)
        await self._client.async_reboot()


class CrestronPduResetAndRebootButton(ButtonEntity):
    """Device-level button matching the real-world fix procedure: reset + reboot in one tap."""

    def __init__(self, client: CrestronPduClient, pdu_host: str, pdu_name: str):
        self._client = client
        self._pdu_host = pdu_host
        self._pdu_name = pdu_name
        self._attr_name = "Reset Protection & Reboot"
        self._attr_unique_id = f"crestron_pdu_{pdu_host}_reset_and_reboot"
        self._attr_icon = "mdi:restart-alert"

    @property
    def device_info(self):
        return {
            "identifiers": {("crestron_home", f"pdu_{self._pdu_host}")},
            "name": f"Crestron PDU ({self._pdu_name})",
            "manufacturer": "Crestron",
            "model": "PC-350V Series",
        }

    def set_available(self, is_available: bool):
        self._attr_available = is_available
        if self.hass is not None:
            self.async_write_ha_state()

    async def async_press(self) -> None:
        _LOGGER.info(
            "Resetting voltage protection and rebooting PDU %s "
            "(device will be offline for ~3.5 minutes)", self._pdu_host
        )
        await self._client.async_reset_and_reboot()
