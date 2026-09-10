import logging
from homeassistant.components.climate import (
    ClimateEntity,
    ClimateEntityFeature,
    HVACMode,
    FAN_AUTO,
    FAN_ON,
)
from homeassistant.const import UnitOfTemperature
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .helpers import get_room_name, assign_device_area

_LOGGER = logging.getLogger(__name__)

# Crestron mode strings -> Home Assistant HVACMode
CRESTRON_TO_HA_HVAC = {
    "off": HVACMode.OFF,
    "heat": HVACMode.HEAT,
    "cool": HVACMode.COOL,
    "auto": HVACMode.HEAT_COOL,
}

# HA HVACMode -> Crestron mode string expected by POST /thermostats/mode
# Per official docs the accepted values are HEAT/COOL/AUTO/OFF (uppercase)
HA_TO_CRESTRON_HVAC = {
    HVACMode.OFF: "OFF",
    HVACMode.HEAT: "HEAT",
    HVACMode.COOL: "COOL",
    HVACMode.HEAT_COOL: "AUTO",
}


async def async_setup_entry(hass, entry, async_add_entities):
    """Set up Crestron Thermostats platform."""
    data = hass.data["crestron_home"][entry.entry_id]
    api, coordinator = data["api"], data["coordinator"]

    thermo_data = (coordinator.data or {}).get("thermostats", {})
    thermostats = thermo_data.get("thermostats", []) if isinstance(thermo_data, dict) else []

    seen_ids = set()
    entities = []
    for t in thermostats:
        thermo_id = str(t.get("id"))
        if thermo_id and thermo_id not in seen_ids:
            seen_ids.add(thermo_id)
            entities.append(CrestronThermostat(coordinator, api, t["id"], t["name"]))

    async_add_entities(entities)


class CrestronThermostat(CoordinatorEntity, ClimateEntity):
    """Representation of a Crestron Thermostat."""

    _attr_temperature_unit = UnitOfTemperature.FAHRENHEIT
    _attr_hvac_modes = [HVACMode.OFF, HVACMode.HEAT, HVACMode.COOL, HVACMode.HEAT_COOL]
    _attr_fan_modes = [FAN_AUTO, FAN_ON]
    _attr_supported_features = (
        ClimateEntityFeature.TARGET_TEMPERATURE
        | ClimateEntityFeature.TARGET_TEMPERATURE_RANGE
        | ClimateEntityFeature.FAN_MODE
    )

    def __init__(self, coordinator, api, thermo_id, name):
        super().__init__(coordinator)
        self._api = api
        self._thermo_id = thermo_id
        self._attr_name = name
        self._attr_unique_id = f"crestron_thermostat_{thermo_id}"

    @property
    def device_info(self):
        info = {
            "identifiers": {("crestron_home", f"thermostat_{self._thermo_id}")},
            "name": self._attr_name,
            "manufacturer": "Crestron",
            "model": "Thermostat",
            "via_device": ("crestron_home", "processor"),
        }
        data = self._get_thermostat_data()
        room_name = get_room_name(self.coordinator, data.get("roomId"))
        if room_name:
            info["suggested_area"] = room_name
        return info

    def _get_thermostat_data(self):
        thermostats = self.coordinator.data.get("thermostats", {}).get("thermostats", [])
        for t in thermostats:
            if str(t.get("id")) == str(self._thermo_id):
                return t
        return {}

    def _parse_temp(self, raw_val):
        """Convert Crestron DeciFahrenheit (e.g. 720) to standard degrees (72.0)."""
        if raw_val is None:
            return None
        try:
            val = float(raw_val)
            if val > 150:
                return round(val / 10.0, 1)
            return val
        except (ValueError, TypeError):
            return None

    def _get_setpoints_list(self, data):
        """
        Normalize setpoints regardless of firmware shape.
        Some firmwares report a single 'setPoint' dict (per official docs).
        Others (confirmed on this system) report a 'currentSetPoint' array.
        """
        sp = data.get("currentSetPoint")
        if isinstance(sp, list):
            return sp
        single = data.get("setPoint")
        if isinstance(single, dict):
            return [single]
        return []

    async def async_added_to_hass(self):
        await super().async_added_to_hass()
        room_name = get_room_name(self.coordinator, self._get_thermostat_data().get("roomId"))
        assign_device_area(self.hass, {("crestron_home", f"thermostat_{self._thermo_id}")}, room_name)

    @property
    def hvac_mode(self) -> HVACMode:
        """Return current HVAC operational mode."""
        data = self._get_thermostat_data()
        # Support both 'mode' (docs) and 'currentMode' (observed on this system)
        mode_raw = str(data.get("currentMode", data.get("mode", "off"))).strip().lower()
        return CRESTRON_TO_HA_HVAC.get(mode_raw, HVACMode.OFF)

    @property
    def current_temperature(self):
        data = self._get_thermostat_data()
        return self._parse_temp(data.get("currentTemperature"))

    @property
    def fan_mode(self):
        data = self._get_thermostat_data()
        fan_raw = str(data.get("currentFanMode", "Auto")).strip()
        return FAN_ON if fan_raw.lower() == "on" else FAN_AUTO

    @property
    def target_temperature(self):
        """Return active target temperature based on current hvac mode."""
        data = self._get_thermostat_data()
        setpoints = self._get_setpoints_list(data)
        current_hvac = self.hvac_mode

        for sp in setpoints:
            sp_type = str(sp.get("type", "")).lower()
            if (current_hvac == HVACMode.COOL and sp_type == "cool") or \
               (current_hvac == HVACMode.HEAT and sp_type == "heat"):
                return self._parse_temp(sp.get("temperature"))

        if setpoints and "temperature" in setpoints[0]:
            return self._parse_temp(setpoints[0]["temperature"])

        return None

    @property
    def target_temperature_high(self):
        data = self._get_thermostat_data()
        for sp in self._get_setpoints_list(data):
            if str(sp.get("type", "")).lower() == "cool":
                return self._parse_temp(sp.get("temperature"))
        return None

    @property
    def target_temperature_low(self):
        data = self._get_thermostat_data()
        for sp in self._get_setpoints_list(data):
            if str(sp.get("type", "")).lower() == "heat":
                return self._parse_temp(sp.get("temperature"))
        return None

    async def async_set_hvac_mode(self, hvac_mode: HVACMode) -> None:
        """
        Set HVAC mode.
        Official endpoint: POST /thermostats/mode
        Body: {"thermostats": [{"id": id, "mode": "HEAT/COOL/AUTO/OFF"}]}
        """
        crestron_mode = HA_TO_CRESTRON_HVAC.get(hvac_mode, "OFF")
        await self._api.request(
            "POST",
            "/thermostats/mode",
            {"thermostats": [{"id": self._thermo_id, "mode": crestron_mode}]},
        )
        await self.coordinator.async_request_refresh()

    async def async_set_fan_mode(self, fan_mode: str) -> None:
        """
        Set fan mode.
        Official endpoint: POST /thermostats/fanmode
        Body: {"thermostats": [{"id": id, "mode": "AUTO/ON"}]}
        """
        c_fan = "ON" if fan_mode == FAN_ON else "AUTO"
        await self._api.request(
            "POST",
            "/thermostats/fanmode",
            {"thermostats": [{"id": self._thermo_id, "mode": c_fan}]},
        )
        await self.coordinator.async_request_refresh()

    async def async_set_temperature(self, **kwargs) -> None:
        """
        Set target setpoint(s).
        Official endpoint: POST /thermostats/SetPoint
        Body: {"id": id, "setpoints": [{"type": "Cool"/"Heat", "temperature": <deci-degrees>}]}
        NOTE: unlike mode/fanmode, this call takes a single 'id', not a 'thermostats' array.
        """
        setpoints = []
        current_hvac = self.hvac_mode

        if "temperature" in kwargs:
            target_deci = int(kwargs["temperature"] * 10)
            if current_hvac == HVACMode.COOL:
                setpoints.append({"type": "Cool", "temperature": target_deci})
            elif current_hvac == HVACMode.HEAT:
                setpoints.append({"type": "Heat", "temperature": target_deci})
            else:
                # Fallback: send as both if mode is unknown/auto
                setpoints.append({"type": "Cool", "temperature": target_deci})

        if "target_temp_high" in kwargs:
            setpoints.append({
                "type": "Cool",
                "temperature": int(kwargs["target_temp_high"] * 10),
            })
        if "target_temp_low" in kwargs:
            setpoints.append({
                "type": "Heat",
                "temperature": int(kwargs["target_temp_low"] * 10),
            })

        if setpoints:
            await self._api.request(
                "POST",
                "/thermostats/SetPoint",
                {"id": self._thermo_id, "setpoints": setpoints},
            )
            await self.coordinator.async_request_refresh()
