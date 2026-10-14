"""Let cloudflared through: Home Assistant's reverse-proxy trust.

🔴 Home Assistant REJECTS a proxied request (one carrying X-Forwarded-For, which
cloudflared always sends) unless its HTTP settings say to use that header and
trust the address the request came from. Without this a healthy tunnel serves a
bare "400: Bad Request" on every path - 1151 East 7th, 2026-10-14.

🔴 From Core 2026.8 the HTTP settings live in a store managed from Settings >
System > Network, not in configuration.yaml. YAML is migrated once, on the
first boot, and an `http:` block added after that is ignored. So the trust is
set here, in the store, the same way that settings page does it:

1. stage it as the *pending* config (`async_set_pending`) and restart;
2. Home Assistant starts on the pending config as a trial, and reverts to the
   last confirmed one if nobody confirms it within five minutes;
3. commissioning runs again on every start, sees itself running on its own
   trial and confirms it (`async_promote_pending`).

Only the trust is changed: the port, SSL and everything else is copied from the
confirmed config, because the tunnel was routed at the port Home Assistant is
on now.

172.30.32.0/23 rather than the 172.30.33.0/24 the cloudflared docs suggest: that
is the whole Supervisor network, and add-ons are allocated across all of it.
"""
from __future__ import annotations

from ipaddress import ip_network
from typing import Any

from homeassistant.core import HomeAssistant

SUPERVISOR_NET = ip_network("172.30.32.0/23")
USE_XFF = "use_x_forwarded_for"
TRUSTED = "trusted_proxies"
ERROR = "error"
# The store's own bookkeeping on a config, never part of what is compared.
_META = ("created_at", "error", "error_message")

# What `ensure` found or did.
ALREADY = "already trusted"
CONFIRMED = "confirmed"
STAGED = "staged"
UNSUPPORTED = "unsupported"
BLOCKED = "blocked"


def trusts(conf: dict | None) -> bool:
    """Does this HTTP config let requests from the Supervisor network through?"""
    if not conf or not conf.get(USE_XFF):
        return False
    for proxy in conf.get(TRUSTED) or []:
        try:
            net = ip_network(str(proxy), strict=False)
        except ValueError:
            continue
        if net.version == SUPERVISOR_NET.version and SUPERVISOR_NET.subnet_of(net):
            return True
    return False


def with_trust(conf: dict) -> dict[str, Any]:
    """`conf` with the trust added and nothing else changed."""
    out = {k: v for k, v in conf.items() if k not in _META}
    out[USE_XFF] = True
    proxies = [str(p) for p in out.get(TRUSTED) or []]
    if str(SUPERVISOR_NET) not in proxies:
        proxies.append(str(SUPERVISOR_NET))
    out[TRUSTED] = proxies
    return out


async def ensure(hass: HomeAssistant, ours: str | None = None,
                 retry_reverted: bool = False) -> tuple[str, str, str | None]:
    """Make Home Assistant trust the Supervisor network.

    Returns (what, detail, staged): `staged` is the `created_at` of the pending
    config this call staged, to be passed back as `ours` on the next run - it is
    how a trial is known to be ours. 🔴 Only our own trial is ever confirmed or
    counted as reverted: a trial somebody started from the settings page, or
    the one Home Assistant made from an old seeded `http:` block, is theirs.

    STAGED means a restart is needed; everything else is final for this run.
    """
    if getattr(hass.config, "recovery_mode", False):
        return BLOCKED, ("Home Assistant is in recovery mode, so its HTTP "
                         "settings are not the ones in use"), None
    try:
        from homeassistant.components.http import config as http_config  # noqa: PLC0415
        load_store = http_config.async_get_and_load_store
    except (ImportError, AttributeError):
        # Before 2026.8 the HTTP settings were configuration.yaml's `http:`,
        # which the BAV House Setup add-on writes.
        return UNSUPPORTED, ("Home Assistant is older than 2026.8, so the trust "
                             "comes from configuration.yaml's http: block"), None

    store = await load_store(hass)
    pending = store.pending
    running_pending = str(store.active_config_type) == "pending"
    mine = bool(pending is not None and ours
                and pending.get("created_at") == ours)

    if pending is not None and running_pending:
        if mine:
            # Our own trial, and Home Assistant came up on it: confirm it
            # before the five-minute revert does the opposite.
            await store.async_promote_pending()
            return CONFIRMED, "trusted proxies confirmed (172.30.32.0/23)", None
        if trusts(store.stable):
            # Somebody is trying a change of their own; it is theirs to
            # confirm, or Home Assistant's to revert. Nothing for us to do.
            return ALREADY, ("Home Assistant already trusts 172.30.32.0/23 "
                             "(an HTTP settings change on trial is left to "
                             "whoever made it)"), None
        return BLOCKED, ("an HTTP settings change made by somebody else is on "
                         "trial; confirm or discard it in Settings > System > "
                         "Network > HTTP server, then run commissioning "
                         "again"), None

    if trusts(store.stable):
        return ALREADY, "Home Assistant already trusts 172.30.32.0/23", None

    if pending is not None:
        if pending.get(ERROR):
            if mine and not retry_reverted:
                # Ours, tried and reverted. Trying again unasked could restart
                # the house in a loop, so a person has to look - running the
                # commissioning action by hand tries once more.
                return BLOCKED, (
                    f"Home Assistant reverted the trusted-proxy setting "
                    f"({pending.get(ERROR)}). Run commissioning again by hand to "
                    f"retry, or set it in Settings > System > Network > HTTP "
                    f"server: Trust X-Forwarded-For on, trusted proxy "
                    f"172.30.32.0/23, and confirm it after the restart"), None
            # Somebody else's failed trial - an old seeded `http:` block that
            # Home Assistant migrated and then reverted is the usual one. It is
            # never applied again; staging over it is what the settings page
            # would do too.
        elif not mine:
            return BLOCKED, ("an HTTP settings change made by somebody else is "
                             "staged for the next restart; leaving it alone. "
                             "Restart, confirm or discard it in Settings > "
                             "System > Network > HTTP server, then run "
                             "commissioning again"), None

    new = with_trust(store.stable)
    # Through Home Assistant's own schema, as its settings page does: it
    # normalises the networks, and a config it would refuse is refused here,
    # before a restart, rather than by a Home Assistant that will not start.
    schema = getattr(http_config, "HTTP_STORAGE_SCHEMA", None)
    if schema is not None:
        new = dict(schema(new))
    await store.async_set_pending(new)
    staged = (store.pending or {}).get("created_at")
    return STAGED, ("trust for 172.30.32.0/23 staged; Home Assistant restarts "
                    "to apply it and commissioning confirms it after the "
                    "restart"), staged
