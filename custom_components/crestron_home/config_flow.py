import voluptuous as vol
from homeassistant import config_entries
from homeassistant.core import callback
from homeassistant.helpers import selector
from .const import DOMAIN, PLATFORMS, DEFAULT_POLLING_INTERVAL
from .api import CrestronHomeAPI

# Helper to build option dicts for the platform selector
PLATFORM_OPTIONS = [{"label": p.replace("_", " ").title(), "value": p} for p in PLATFORMS]


class CrestronHomeFlowHandler(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle a config flow for Crestron Home."""

    VERSION = 1

    async def async_step_user(self, user_input=None):
        """Handle the initial setup step initiated by the user."""
        errors = {}
        if user_input is not None:
            api = CrestronHomeAPI(user_input["host"], user_input["token"])
            if await api.login():
                return self.async_create_entry(
                    title=f"Crestron Home ({user_input['host']})",
                    data=user_input
                )
            errors["base"] = "cannot_connect"

        data_schema = vol.Schema({
            vol.Required("host"): str,
            vol.Required("token"): str,
            vol.Required("platforms", default=PLATFORMS): selector.SelectSelector(
                selector.SelectSelectorConfig(
                    options=PLATFORM_OPTIONS,
                    multiple=True,
                    mode=selector.SelectSelectorMode.LIST,
                )
            ),
        })
        return self.async_show_form(
            step_id="user",
            data_schema=data_schema,
            errors=errors
        )

    @staticmethod
    @callback
    def async_get_options_flow(config_entry):
        """Link the Options Flow to the integration."""
        return CrestronHomeOptionsFlowHandler()


class CrestronHomeOptionsFlowHandler(config_entries.OptionsFlow):
    """
    Handle the 'Configure' menu for later changes.

    Supports adding/removing any number of PDUs (PC-350 series or similar)
    via a menu, rather than a single fixed set of PDU fields. PDUs are
    stored as a list of dicts under the "pdus" option key:
        [{"host": ..., "username": ..., "password": ...}, ...]
    """

    def __init__(self):
        # Working copies, populated lazily from the config entry the first
        # time each is needed, then carried across steps in this flow.
        self._platforms = None
        self._polling_interval = None
        self._pdus = None

    def _ensure_loaded(self):
        """Load current settings from the config entry into working state, once."""
        if self._platforms is None:
            self._platforms = list(self.config_entry.options.get(
                "platforms", self.config_entry.data.get("platforms", PLATFORMS)
            ))
        if self._polling_interval is None:
            self._polling_interval = self.config_entry.options.get(
                "polling_interval", DEFAULT_POLLING_INTERVAL
            )
        if self._pdus is None:
            # Backward compatibility: migrate the old single pdu_host/
            # pdu_username/pdu_password fields into the new list format
            # if present and the list hasn't been set up yet.
            existing_list = self.config_entry.options.get("pdus")
            if existing_list is not None:
                self._pdus = list(existing_list)
            else:
                legacy_host = self.config_entry.options.get(
                    "pdu_host", self.config_entry.data.get("pdu_host")
                )
                if legacy_host:
                    self._pdus = [{
                        "name": legacy_host,
                        "host": legacy_host,
                        "username": self.config_entry.options.get(
                            "pdu_username", self.config_entry.data.get("pdu_username", "")
                        ),
                        "password": self.config_entry.options.get(
                            "pdu_password", self.config_entry.data.get("pdu_password", "")
                        ),
                    }]
                else:
                    self._pdus = []

    async def async_step_init(self, user_input=None):
        """Show the main menu."""
        self._ensure_loaded()

        menu_options = {
            "general": "General Settings (platforms, polling)",
            "add_pdu": "Add a PDU (PC-350 or similar)",
        }
        if self._pdus:
            menu_options["remove_pdu"] = f"Remove a PDU ({len(self._pdus)} configured)"
        menu_options["finish"] = "Save and Close"

        return self.async_show_menu(step_id="init", menu_options=menu_options)

    async def async_step_general(self, user_input=None):
        """Platforms + polling interval - unrelated to PDUs."""
        if user_input is not None:
            self._platforms = user_input["platforms"]
            self._polling_interval = user_input["polling_interval"]
            return await self.async_step_init()

        schema = vol.Schema({
            vol.Required("platforms", default=self._platforms): selector.SelectSelector(
                selector.SelectSelectorConfig(
                    options=PLATFORM_OPTIONS,
                    multiple=True,
                    mode=selector.SelectSelectorMode.LIST,
                )
            ),
            vol.Required("polling_interval", default=self._polling_interval): vol.All(
                vol.Coerce(int), vol.Range(min=1, max=300)
            ),
        })
        return self.async_show_form(step_id="general", data_schema=schema)

    async def async_step_add_pdu(self, user_input=None):
        """Add a single PDU to the list."""
        errors = {}
        if user_input is not None:
            if not user_input["host"]:
                errors["host"] = "required"
            else:
                self._pdus.append({
                    "name": user_input["name"] or user_input["host"],
                    "host": user_input["host"],
                    "username": user_input["username"],
                    "password": user_input["password"],
                })
                return await self.async_step_init()

        schema = vol.Schema({
            vol.Optional("name", default=""): str,
            vol.Required("host"): str,
            vol.Required("username"): str,
            vol.Required("password"): selector.TextSelector(
                selector.TextSelectorConfig(type=selector.TextSelectorType.PASSWORD)
            ),
        })
        return self.async_show_form(step_id="add_pdu", data_schema=schema, errors=errors)

    async def async_step_remove_pdu(self, user_input=None):
        """Remove one or more PDUs from the list, selected by name/host."""
        if user_input is not None:
            hosts_to_remove = set(user_input["pdus_to_remove"])
            self._pdus = [
                p for p in self._pdus
                if f"{p.get('name', p['host'])} - {p['host']}" not in hosts_to_remove
            ]
            return await self.async_step_init()

        pdu_labels = [f"{p.get('name', p['host'])} - {p['host']}" for p in self._pdus]
        schema = vol.Schema({
            vol.Required("pdus_to_remove"): selector.SelectSelector(
                selector.SelectSelectorConfig(
                    options=pdu_labels,
                    multiple=True,
                    mode=selector.SelectSelectorMode.LIST,
                )
            ),
        })
        return self.async_show_form(step_id="remove_pdu", data_schema=schema)

    async def async_step_finish(self, user_input=None):
        """Save everything collected across the menu steps and close."""
        self._ensure_loaded()
        return self.async_create_entry(
            title="",
            data={
                "platforms": self._platforms,
                "polling_interval": self._polling_interval,
                "pdus": self._pdus,
            },
        )
