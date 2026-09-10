"""The commissioning form.

Four short steps rather than one long one, because the failure modes are
different and each should be caught where it was typed: the site, the
processor (validated against the real thing), Cloudflare, and what to install.

The Cloudflare step is the one that carries fleet-wide credentials. They are
read from a file the image seeder leaves behind, shown as already-answered, and
only asked for if there is no file or somebody presses Change - which is what
makes commissioning a seeded box a matter of confirming four forms.
"""
from __future__ import annotations

import json
import logging
import os
import re

import aiohttp
import voluptuous as vol

from homeassistant import config_entries
from homeassistant.core import callback
from homeassistant.helpers import selector
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .cloudflare import Cloudflare, CloudflareError
from .const import (
    ADDON_CHOICES,
    CONF_ADDONS,
    CONF_CF_ACCOUNT_ID,
    CONF_CF_API_TOKEN,
    CONF_CF_TUNNEL_NAME,
    CONF_CF_ZONE,
    CONF_CHANGE_CF,
    CONF_CRESTRON_HOST,
    CONF_CRESTRON_TOKEN,
    CONF_DEVICE_PIN,
    CONF_HOSTNAME,
    CONF_SITE_NAME,
    CONF_SITE_SLUG,
    DEFAULT_DEVICE_PIN,
    DOMAIN,
    FLEET_DEFAULTS_PATH,
)

_LOGGER = logging.getLogger(__name__)


def slugify(name: str) -> str:
    """`14 Malke Drive` -> `14malke`. The shape HomeUI already uses for a site."""
    words = re.findall(r"[a-z0-9]+", (name or "").lower())
    drop = {"drive", "dr", "street", "st", "road", "rd", "avenue", "ave",
            "lane", "ln", "court", "ct", "place", "pl", "way", "the"}
    kept = [w for w in words if w not in drop] or words
    return "".join(kept)[:32]


def read_fleet_defaults(config_dir: str) -> dict:
    """Credentials the whole fleet shares, left by the image seeder.

    Deliberately not in the add-on repository: that repository is public, and a
    Cloudflare token with tunnel and DNS write would let anyone stand up a
    hostname in the zone. A file on the box is only as exposed as the box.
    """
    path = os.path.join(config_dir, FLEET_DEFAULTS_PATH)
    try:
        with open(path, encoding="utf-8") as handle:
            data = json.load(handle)
        return data if isinstance(data, dict) else {}
    except FileNotFoundError:
        return {}
    except (OSError, ValueError) as err:
        _LOGGER.warning("bav_house: %s could not be read: %s", path, err)
        return {}


async def check_processor(session: aiohttp.ClientSession, host: str,
                          token: str) -> str | None:
    """None if the processor accepted the token, else why not.

    The same handshake the Crestron Home integration does - done here so a wrong
    token is caught on the form that asked for it, rather than as a broken
    integration twenty minutes later.
    """
    url = f"https://{host}:443/cws/api/login"
    try:
        async with session.get(
            url, headers={"Crestron-RestAPI-AuthToken": token},
            ssl=False, timeout=aiohttp.ClientTimeout(total=15),
        ) as resp:
            if resp.status != 200:
                return "invalid_auth" if resp.status in (401, 403) else "cannot_connect"
            body = await resp.json(content_type=None)
            key = (body or {}).get("authkey") or (body or {}).get("AuthKey")
            return None if key else "invalid_auth"
    except Exception:  # noqa: BLE001 - unreachable, TLS, DNS all mean the same to the form
        return "cannot_connect"


class BavHouseFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Commission one house."""

    VERSION = 1

    def __init__(self) -> None:
        self._data: dict = {}
        self._fleet: dict = {}

    async def _load_fleet(self) -> None:
        if not self._fleet:
            self._fleet = await self.hass.async_add_executor_job(
                read_fleet_defaults, self.hass.config.config_dir)

    async def async_step_integration_discovery(self, discovery_info=None):
        """Raised by the integration itself on a box that has never been set up,
        so a freshly imaged house offers to commission itself.

        A second one cannot happen: `single_config_entry` in the manifest makes
        Home Assistant refuse the flow outright once a house is set up - one box
        is one house, and enforcing that here as well would only be a second
        place for it to be wrong."""
        self.context["title_placeholders"] = {"name": "this house"}
        return await self.async_step_user()

    async def async_step_user(self, user_input=None):
        await self._load_fleet()
        errors: dict[str, str] = {}
        zone = self._fleet.get("zone") or "bav.homes"
        if user_input is not None:
            name = (user_input[CONF_SITE_NAME] or "").strip()
            slug = slugify(name)
            if not slug:
                errors[CONF_SITE_NAME] = "bad_site_name"
            else:
                hostname = (user_input.get(CONF_HOSTNAME) or "").strip().lower()
                self._data.update({
                    CONF_SITE_NAME: name,
                    CONF_SITE_SLUG: slug,
                    CONF_HOSTNAME: hostname or f"{slug}.{zone}",
                    CONF_DEVICE_PIN: user_input[CONF_DEVICE_PIN],
                })
                return await self.async_step_crestron()

        current = user_input or {}
        schema = vol.Schema({
            vol.Required(CONF_SITE_NAME,
                         default=current.get(CONF_SITE_NAME, "")): str,
            vol.Optional(CONF_HOSTNAME,
                         default=current.get(CONF_HOSTNAME, "")): str,
            vol.Required(CONF_DEVICE_PIN,
                         default=self._fleet.get("device_pin")
                         or DEFAULT_DEVICE_PIN): str,
        })
        return self.async_show_form(
            step_id="user", data_schema=schema, errors=errors,
            description_placeholders={"zone": zone})

    async def async_step_crestron(self, user_input=None):
        errors: dict[str, str] = {}
        if user_input is not None:
            host = (user_input[CONF_CRESTRON_HOST] or "").strip()
            token = (user_input[CONF_CRESTRON_TOKEN] or "").strip()
            if user_input.get("skip"):
                self._data.update({CONF_CRESTRON_HOST: "", CONF_CRESTRON_TOKEN: ""})
                return await self.async_step_cloudflare()
            why = await check_processor(async_get_clientsession(self.hass),
                                        host, token)
            if why:
                errors["base"] = why
            else:
                self._data.update({CONF_CRESTRON_HOST: host,
                                   CONF_CRESTRON_TOKEN: token})
                return await self.async_step_cloudflare()

        current = user_input or {}
        schema = vol.Schema({
            vol.Required(CONF_CRESTRON_HOST,
                         default=current.get(CONF_CRESTRON_HOST, "")): str,
            vol.Required(CONF_CRESTRON_TOKEN,
                         default=current.get(CONF_CRESTRON_TOKEN, "")):
                selector.TextSelector(selector.TextSelectorConfig(
                    type=selector.TextSelectorType.PASSWORD)),
            vol.Optional("skip", default=False): bool,
        })
        return self.async_show_form(step_id="crestron", data_schema=schema,
                                    errors=errors)

    async def async_step_cloudflare(self, user_input=None):
        await self._load_fleet()
        have = bool(self._fleet.get("cf_api_token")
                    and self._fleet.get("cf_account_id"))
        if user_input is not None:
            self._data[CONF_CF_TUNNEL_NAME] = (
                user_input.get(CONF_CF_TUNNEL_NAME)
                or self._data[CONF_SITE_SLUG])
            if user_input.get(CONF_CHANGE_CF) or not have:
                return await self.async_step_cloudflare_account()
            self._data.update({
                CONF_CF_API_TOKEN: self._fleet["cf_api_token"],
                CONF_CF_ACCOUNT_ID: self._fleet["cf_account_id"],
                CONF_CF_ZONE: self._fleet.get("zone") or "bav.homes",
            })
            return await self.async_step_addons()

        schema = vol.Schema({
            vol.Required(CONF_CF_TUNNEL_NAME,
                         default=self._data.get(CONF_SITE_SLUG, "")): str,
            vol.Optional(CONF_CHANGE_CF, default=not have): bool,
        })
        account = self._fleet.get("cf_account_id") or ""
        return self.async_show_form(
            step_id="cloudflare", data_schema=schema,
            description_placeholders={
                "hostname": self._data.get(CONF_HOSTNAME, ""),
                "account": f"…{account[-6:]}" if account else "not set on this box",
            })

    async def async_step_cloudflare_account(self, user_input=None):
        """Only shown when there is nothing seeded, or Change was ticked."""
        errors: dict[str, str] = {}
        if user_input is not None:
            token = (user_input.get(CONF_CF_API_TOKEN) or "").strip()
            account = (user_input.get(CONF_CF_ACCOUNT_ID) or "").strip()
            zone = (user_input.get(CONF_CF_ZONE) or "").strip()
            if not token or not account:
                # Left blank on purpose: no tunnel, everything else still runs.
                self._data.update({CONF_CF_API_TOKEN: "", CONF_CF_ACCOUNT_ID: "",
                                   CONF_CF_ZONE: zone})
                return await self.async_step_addons()
            cf = Cloudflare(async_get_clientsession(self.hass), token, account)
            try:
                await cf.verify()
            except CloudflareError as err:
                _LOGGER.debug("bav_house: cloudflare check failed: %s", err)
                errors["base"] = "cloudflare_auth"
            else:
                self._data.update({CONF_CF_API_TOKEN: token,
                                   CONF_CF_ACCOUNT_ID: account,
                                   CONF_CF_ZONE: zone})
                return await self.async_step_addons()

        current = user_input or {}
        schema = vol.Schema({
            vol.Optional(CONF_CF_API_TOKEN,
                         default=current.get(CONF_CF_API_TOKEN,
                                             self._fleet.get("cf_api_token", ""))):
                selector.TextSelector(selector.TextSelectorConfig(
                    type=selector.TextSelectorType.PASSWORD)),
            vol.Optional(CONF_CF_ACCOUNT_ID,
                         default=current.get(CONF_CF_ACCOUNT_ID,
                                             self._fleet.get("cf_account_id", ""))): str,
            vol.Optional(CONF_CF_ZONE,
                         default=current.get(CONF_CF_ZONE,
                                             self._fleet.get("zone", "bav.homes"))): str,
        })
        return self.async_show_form(step_id="cloudflare_account",
                                    data_schema=schema, errors=errors)

    async def async_step_addons(self, user_input=None):
        if user_input is not None:
            self._data[CONF_ADDONS] = user_input.get(CONF_ADDONS) or []
            return self.async_create_entry(
                title=self._data[CONF_SITE_NAME], data=self._data)

        options = [selector.SelectOptionDict(value=slug, label=label)
                   for slug, label, _ in ADDON_CHOICES]
        default = [slug for slug, _, on in ADDON_CHOICES if on]
        if not self._data.get(CONF_CF_API_TOKEN):
            default = [s for s in default if s != "cloudflared"]
        schema = vol.Schema({
            vol.Optional(CONF_ADDONS, default=default): selector.SelectSelector(
                selector.SelectSelectorConfig(options=options, multiple=True,
                                              mode=selector.SelectSelectorMode.LIST)),
        })
        return self.async_show_form(step_id="addons", data_schema=schema)

    @staticmethod
    @callback
    def async_get_options_flow(config_entry):
        return BavHouseOptionsFlow()


class BavHouseOptionsFlow(config_entries.OptionsFlow):
    """Change what is installed, or re-run commissioning after fixing something."""

    async def async_step_init(self, user_input=None):
        if user_input is not None:
            return self.async_create_entry(title="", data={
                **{k: v for k, v in self.config_entry.options.items()},
                CONF_ADDONS: user_input.get(CONF_ADDONS) or [],
            })
        options = [selector.SelectOptionDict(value=slug, label=label)
                   for slug, label, _ in ADDON_CHOICES]
        current = (self.config_entry.options.get(CONF_ADDONS)
                   or self.config_entry.data.get(CONF_ADDONS) or [])
        schema = vol.Schema({
            vol.Optional(CONF_ADDONS, default=list(current)):
                selector.SelectSelector(selector.SelectSelectorConfig(
                    options=options, multiple=True,
                    mode=selector.SelectSelectorMode.LIST)),
        })
        return self.async_show_form(step_id="init", data_schema=schema)
