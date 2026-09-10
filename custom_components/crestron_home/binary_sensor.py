import logging
from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
)
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .helpers import get_room_name, assign_device_area
from .pdu_api import CrestronPduClient

_LOGGER = logging.getLogger(__name__)

# Values observed/documented for the 'presence' field that mean "active"
# (occupied / open / on), vs. "inactive" (vacant / closed / off).
# Crestron firmware has shown both simple values ("Open", "Present") and
# combined ones ("OpenOrOn", "CloseOrOff") - normalize and check both.
ACTIVE_VALUES = {"present", "occupied", "open", "on", "openoron"}
INACTIVE_VALUES = {"vacant", "unavailable", "closed", "close", "off", "closeoroff"}

# PDU fault fields, verified from a real captured /Device response under
# Device.PowerController.Faults. Maps the field name to a display name.
PDU_FAULT_FIELDS = {
    "IsOverVoltageFaultDetected": "Over Voltage Fault",
    "IsUnderVoltageFaultDetected": "Under Voltage Fault",
    "IsOverCurrentFaultDetected": "Over Current Fault",
    "IsWiringFaultDetected": "Wiring Fault",
    "IsSurgeCompromised": "Surge Protection Compromised",
}


def _presence_is_active(raw_value) -> bool:
    normalized = str(raw_value or "").strip().lower().replace(" ", "").replace("_", "")
    if normalized in ACTIVE_VALUES:
        return True
    if normalized in INACTIVE_VALUES:
        return False
    # Unknown value - log it so we can add it to the map, default to False
    _LOGGER.warning("Unrecognized Crestron sensor presence value: %r", raw_value)
    return False


async def async_setup_entry(hass, entry, async_add_entities):
    """Set up Crestron Binary Sensors platform (Crestron Home sensors + PDU faults)."""
    data = hass.data["crestron_home"][entry.entry_id]
    api, coordinator = data["api"], data["coordinator"]

    sensors_data = (coordinator.data or {}).get("sensors", {})
    sensors = sensors_data.get("sensors", []) if isinstance(sensors_data, dict) else []

    entities = []
    for sensor in sensors:
        sub_type = str(sensor.get("subType", "")).strip().lower()
        # PhotoSensor reports a numeric light "level", not a binary state -
        # that belongs in sensor.py, not here. Everything else with a
        # 'presence' field is fair game for a binary sensor.
        if sub_type == "photosensor":
            continue
        entities.append(
            CrestronBinarySensor(coordinator, api, sensor["id"], sensor["name"], sub_type)
        )

    # PDU fault sensors - uses the SAME shared, already-connected client
    # from __init__.py that switch.py and button.py also use, instead of
    # opening yet another separate connection to the same PDU.
    pdu_clients = data.get("pdu_clients", {})

    for pdu_host, pdu_info in pdu_clients.items():
        client: CrestronPduClient = pdu_info["client"]
        pdu_name = pdu_info["name"]
        full_state = pdu_info["initial_state"]

        faults_data = full_state.get("Device", {}).get("PowerController", {}).get("Faults", {})

        pdu_fault_sensors = []
        for field_name, display_name in PDU_FAULT_FIELDS.items():
            sensor = CrestronPduFaultSensor(client, pdu_host, pdu_name, field_name, display_name)
            sensor.set_state(faults_data.get(field_name, False))
            sensor.set_available(client.is_connected)
            entities.append(sensor)
            pdu_fault_sensors.append(sensor)

        def make_push_handler(fault_sensors):
            def handle_push(device_partial: dict):
                faults = device_partial.get("PowerController", {}).get("Faults", {})
                for sensor in fault_sensors:
                    if sensor.field_name in faults:
                        sensor.set_state(faults[sensor.field_name])
            return handle_push

        client.add_state_listener(make_push_handler(pdu_fault_sensors))

        def make_availability_handler(fault_sensors):
            def handle_availability(is_connected: bool):
                for sensor in fault_sensors:
                    sensor.set_available(is_connected)
            return handle_availability

        client.add_availability_listener(make_availability_handler(pdu_fault_sensors))

    async_add_entities(entities)


class CrestronBinarySensor(CoordinatorEntity, BinarySensorEntity):
    """Representation of a Crestron occupancy/door/window/motion sensor."""

    def __init__(self, coordinator, api, sensor_id, name, sensor_type):
        super().__init__(coordinator)
        self._api = api
        self._sensor_id = sensor_id
        self._attr_name = name
        self._attr_unique_id = f"crestron_sensor_{sensor_id}"
        self._sensor_type = sensor_type

        if "motion" in self._sensor_type:
            self._attr_device_class = BinarySensorDeviceClass.MOTION
        elif "occupancy" in self._sensor_type:
            self._attr_device_class = BinarySensorDeviceClass.OCCUPANCY
        elif "window" in self._sensor_type:
            self._attr_device_class = BinarySensorDeviceClass.WINDOW
        elif "door" in self._sensor_type or "contact" in self._sensor_type:
            self._attr_device_class = BinarySensorDeviceClass.DOOR
        else:
            self._attr_device_class = None

    def _get_sensor_data(self):
        sensors = self.coordinator.data.get("sensors", {}).get("sensors", [])
        for s in sensors:
            if str(s.get("id")) == str(self._sensor_id):
                return s
        return {}

    @property
    def device_info(self):
        info = {
            "identifiers": {("crestron_home", f"sensor_{self._sensor_id}")},
            "name": self._attr_name,
            "manufacturer": "Crestron",
            "model": "Sensor",
            "via_device": ("crestron_home", "processor"),
        }
        room_name = get_room_name(self.coordinator, self._get_sensor_data().get("roomId"))
        if room_name:
            info["suggested_area"] = room_name
        return info

    async def async_added_to_hass(self):
        await super().async_added_to_hass()
        room_name = get_room_name(self.coordinator, self._get_sensor_data().get("roomId"))
        assign_device_area(self.hass, {("crestron_home", f"sensor_{self._sensor_id}")}, room_name)

    @property
    def is_on(self) -> bool:
        """
        Return true if the sensor is in its 'active' state (occupied/open/on).

        Every sensor subtype on this system reports through the same
        'presence' field - there is no separate 'door status' key despite
        what the public docs sample showed.
        """
        data = self._get_sensor_data()
        return _presence_is_active(data.get("presence"))

    @property
    def extra_state_attributes(self):
        data = self._get_sensor_data()
        room_name = get_room_name(self.coordinator, data.get("roomId")) or "Unknown Room"
        attrs = {"crestron_room": room_name, "crestron_sub_type": data.get("subType")}

        battery = data.get("battery level")
        if battery is not None:
            attrs["battery_level"] = battery

        return attrs


class CrestronPduFaultSensor(BinarySensorEntity):
    """
    A fault indicator for a PDU (over/under voltage, over current, wiring,
    surge). Verified against a real captured /Device response under
    Device.PowerController.Faults, and kept live via the same WebSocket
    push stream used for outlet state and metering.

    Uses the "problem" device class: on = a problem is currently detected.
    """

    _attr_should_poll = False

    def __init__(self, client: CrestronPduClient, pdu_host: str, pdu_name: str, field_name: str, display_name: str):
        self._client = client
        self._pdu_host = pdu_host
        self._pdu_name = pdu_name
        self.field_name = field_name
        self._attr_name = display_name
        self._attr_unique_id = f"crestron_pdu_{pdu_host}_{field_name.lower()}"
        self._attr_device_class = BinarySensorDeviceClass.PROBLEM
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

    def set_state(self, is_on: bool):
        self._is_on = is_on
        if self.hass is not None:
            self.async_write_ha_state()

    def set_available(self, is_available: bool):
        self._attr_available = is_available
        if self.hass is not None:
            self.async_write_ha_state()
