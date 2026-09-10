"""Enough of the Cloudflare API to give a house a name on the internet.

The cloudflared add-on can be pointed at a tunnel in two ways. `tunnel_name`
plus `external_hostname` makes the add-on create the tunnel itself - and that
route prints a URL in the add-on log that somebody has to open in a browser and
authorise. That is fine once and unacceptable across a fleet.

`tunnel_token` skips it entirely: the tunnel already exists, the token proves
which one, and the add-on connects unattended. So this module makes the tunnel
first - naming it, routing it at Home Assistant, and pointing a DNS record at
it - and hands the token over. Nobody opens a browser.

Every call is idempotent by lookup-then-create, because commissioning is re-run
and a second run must not leave a house with two tunnels of the same name.
"""
from __future__ import annotations

import asyncio
import logging

import aiohttp

_LOGGER = logging.getLogger(__name__)

API = "https://api.cloudflare.com/client/v4"
TIMEOUT = aiohttp.ClientTimeout(total=30)

# Home Assistant as the cloudflared container sees it. The add-on shares Core's
# Docker network, where Core answers to this name.
HA_SERVICE = "http://homeassistant:8123"


class CloudflareError(Exception):
    """Cloudflare refused, or could not be reached."""


class Cloudflare:
    def __init__(self, session: aiohttp.ClientSession, api_token: str,
                 account_id: str) -> None:
        self._session = session
        self._token = api_token
        self._account = account_id

    async def _call(self, method: str, path: str, body: dict | None = None) -> dict:
        try:
            async with self._session.request(
                method, API + path, json=body, timeout=TIMEOUT,
                headers={"Authorization": f"Bearer {self._token}"},
            ) as resp:
                try:
                    payload = await resp.json(content_type=None)
                except ValueError:
                    payload = {}
                if not isinstance(payload, dict):
                    payload = {}
                if resp.status >= 400 or not payload.get("success", False):
                    raise CloudflareError(
                        f"{method} {path}: {resp.status} {_why(payload)}"
                    )
                return payload
        except (aiohttp.ClientError, asyncio.TimeoutError) as err:
            raise CloudflareError(f"{method} {path}: {err!r}") from err

    async def verify(self) -> None:
        """Prove the token works and can see this account, before the form is
        accepted - a wrong token found at commissioning time is a typo, and a
        wrong token found later is a call-out."""
        await self._call("GET", f"/accounts/{self._account}/cfd_tunnel?per_page=1")

    # -- tunnel ----------------------------------------------------------

    async def find_tunnel(self, name: str) -> dict | None:
        """The tunnel called `name`, matched exactly first, then ignoring case.

        🔴 The fleet's tunnels are named by hand and the case is not
        consistent - `14Malke`, `714Dow`, `882vancourt`, `110roosevelt`. A
        derived name is lowercase, so an exact-only match would look at
        `14Malke`, decide the house has no tunnel, and make a SECOND one. Two
        tunnels for one house is a mess that is only noticed when the wrong one
        is healthy.
        """
        payload = await self._call(
            "GET",
            f"/accounts/{self._account}/cfd_tunnel?is_deleted=false&per_page=1000",
        )
        tunnels = payload.get("result") or []
        for tunnel in tunnels:
            if tunnel.get("name") == name:
                return tunnel
        folded = name.casefold()
        for tunnel in tunnels:
            if (tunnel.get("name") or "").casefold() == folded:
                return tunnel
        return None

    async def ensure_tunnel(self, name: str) -> tuple[str, str]:
        """The tunnel called `name`, made if it is not there. (id, token).

        `config_src: cloudflare` means the routing lives in Cloudflare rather
        than in a YAML file next to the connector - which is what lets the
        add-on run on nothing but a token, and what lets `set_ingress` below
        change a house's routing without touching the house.
        """
        existing = await self.find_tunnel(name)
        if existing:
            tunnel_id = existing["id"]
            return tunnel_id, await self.tunnel_token(tunnel_id)
        payload = await self._call(
            "POST", f"/accounts/{self._account}/cfd_tunnel",
            {"name": name, "config_src": "cloudflare"},
        )
        result = payload.get("result") or {}
        tunnel_id, token = result.get("id"), result.get("token")
        if not tunnel_id or not token:
            raise CloudflareError(f"tunnel created but no id/token returned: {result}")
        return tunnel_id, token

    async def tunnel_token(self, tunnel_id: str) -> str:
        payload = await self._call(
            "GET", f"/accounts/{self._account}/cfd_tunnel/{tunnel_id}/token")
        token = payload.get("result")
        if not isinstance(token, str) or not token:
            raise CloudflareError("no connector token returned for the tunnel")
        return token

    async def get_ingress(self, tunnel_id: str) -> list[dict]:
        payload = await self._call(
            "GET", f"/accounts/{self._account}/cfd_tunnel/{tunnel_id}/configurations")
        config = (payload.get("result") or {}).get("config") or {}
        rules = config.get("ingress")
        return list(rules) if isinstance(rules, list) else []

    async def set_ingress(self, tunnel_id: str, hostname: str) -> str:
        """Route `hostname` at Home Assistant, leaving every other rule alone.

        🔴 A tunnel's configuration is written WHOLE - there is no per-rule
        API - so any rule not sent back is deleted. Real house tunnels carry
        several: a second Home Assistant port, a UniFi console, a processor's
        log page, an SSH route. Replacing the list with just ours would destroy
        them silently, and the tunnel would still report healthy.

        So: keep every existing hostname rule that is not ours, in its original
        order, add or replace ours, and end with the catch-all the API requires
        (preserving whatever the catch-all already was - it is not always a
        404).
        """
        existing = await self.get_ingress(tunnel_id)
        kept, catch_all = [], {"service": "http_status:404"}
        for rule in existing:
            if not rule.get("hostname"):
                # A rule with no hostname is the catch-all; keep its service.
                catch_all = dict(rule)
                continue
            if rule.get("hostname") != hostname:
                kept.append(rule)
        ours = {"hostname": hostname, "service": HA_SERVICE, "originRequest": {}}
        rules = [*kept, ours, catch_all]
        await self._call(
            "PUT", f"/accounts/{self._account}/cfd_tunnel/{tunnel_id}/configurations",
            {"config": {"ingress": rules}},
        )
        return f"{len(kept)} other route(s) kept"

    # -- dns -------------------------------------------------------------

    async def zone_id(self, zone: str) -> str:
        payload = await self._call("GET", f"/zones?name={_q(zone)}")
        for found in payload.get("result") or []:
            if found.get("name") == zone:
                return found["id"]
        raise CloudflareError(
            f"this token cannot see a zone called {zone!r} - check the zone name "
            f"and that the token has DNS edit on it"
        )

    async def ensure_cname(self, zone: str, hostname: str, tunnel_id: str) -> str:
        """Point `hostname` at the tunnel. Returns what was done, for the log.

        An existing record is updated rather than duplicated - re-pointing a
        house at a rebuilt tunnel is a normal thing to have to do.
        """
        zid = await self.zone_id(zone)
        content = f"{tunnel_id}.cfargotunnel.com"
        record = {"type": "CNAME", "name": hostname, "content": content,
                  "proxied": True, "ttl": 1}
        payload = await self._call(
            "GET", f"/zones/{zid}/dns_records?name={_q(hostname)}")
        for existing in payload.get("result") or []:
            if existing.get("name") != hostname:
                continue
            if (existing.get("type") == "CNAME"
                    and existing.get("content") == content
                    and existing.get("proxied")):
                return "already correct"
            await self._call("PUT", f"/zones/{zid}/dns_records/{existing['id']}",
                             record)
            return "updated"
        await self._call("POST", f"/zones/{zid}/dns_records", record)
        return "created"


def _why(payload: dict) -> str:
    errors = payload.get("errors") or []
    parts = [
        f"{e.get('code', '')} {e.get('message', '')}".strip()
        for e in errors if isinstance(e, dict)
    ]
    return "; ".join(p for p in parts if p) or str(payload)[:200]


def _q(value: str) -> str:
    from urllib.parse import quote
    return quote(value, safe="")
