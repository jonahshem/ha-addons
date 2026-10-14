"""Commissioning a house, as a state machine that can be run twice.

The order matters: the tunnel has to exist before cloudflared can be told which
tunnel it is, and the repositories have to be in the store before an add-on in
them can be installed. Beyond that, each step records itself as done on the
config entry, so a run stopped by a restart, a network drop or a wrong password
picks up where it left off instead of re-installing what is already installed.

Nothing here raises out of `run`. A house half-commissioned is the normal
outcome of a bad credential, and it should leave a legible status and a repair
issue rather than a traceback in a log nobody reads.
"""
from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field

import aiohttp
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from . import proxy
from .cloudflare import HA_SERVICE as CF_HA_SERVICE, Cloudflare, CloudflareError
from .const import (
    ADDON_CLOUDFLARED,
    ADDON_NAX_PROBE,
    ADDON_NAX_SENDER,
    ADDON_RAVA_BRIDGE,
    BAV_ADDON_REPO,
    CLOUDFLARED_ADDON_REPO,
    CONF_ADDONS,
    CONF_CF_ACCOUNT_ID,
    CONF_CF_API_TOKEN,
    CONF_CF_TUNNEL_NAME,
    CONF_CF_ZONE,
    CONF_CRESTRON_HOST,
    CONF_CRESTRON_TOKEN,
    CONF_ACCESS_TOKEN,
    CONF_DEVICE_PIN,
    CONF_HOSTNAME,
    CONF_NAX_HOST,
    CONF_NAX_PASSWORD,
    CONF_PROTECT_API_KEY,
    CONF_UNIFI_HOST,
    CRESTRON_DOMAIN,
    DEFAULT_DEVICE_PIN,
    PROXY_RESTART_GUARD,
    STATE_PROXY_RESTART,
    STATE_PROXY_STAGED,
    STATE_STEPS,
    STATE_TUNNEL_ID,
    STEP_ADDONS,
    STEP_CLOUDFLARE,
    STEP_CRESTRON,
    STEP_PROXY,
    STEP_PUBLIC,
    STEP_REPOS,
)
from .supervisor import Supervisor, SupervisorError, SupervisorUnavailable

_LOGGER = logging.getLogger(__name__)


@dataclass
class Progress:
    """What the last (or running) commissioning did, for the UI to show."""

    running: bool = False
    started: float = 0.0
    finished: float = 0.0
    step: str = ""
    lines: list[dict] = field(default_factory=list)
    error: str = ""

    def say(self, step: str, state: str, detail: str = "") -> None:
        self.step = step
        self.lines.append({"step": step, "state": state, "detail": detail,
                           "at": time.time()})
        level = _LOGGER.error if state == "failed" else _LOGGER.info
        level("bav_house: %s %s %s", step, state, detail)

    @property
    def ok(self) -> bool:
        return not self.error and not self.running and bool(self.finished)


def addon_options(slug: str, cfg: dict, tunnel_token: str | None) -> dict:
    """What to write into an add-on's own options for this house.

    Only the fields commissioning actually knows about. `Supervisor.set_options`
    merges, so everything else the add-on ships with, or a person has tuned, is
    left exactly as it is.
    """
    pin = cfg.get(CONF_DEVICE_PIN) or DEFAULT_DEVICE_PIN
    processor = cfg.get(CONF_CRESTRON_HOST) or ""
    if slug == ADDON_CLOUDFLARED:
        # With a token set, the add-on ignores every other option and takes its
        # routing from Cloudflare. `external_hostname` is written anyway so the
        # add-on's own page says which house this is.
        options = {"external_hostname": cfg.get(CONF_HOSTNAME) or ""}
        if tunnel_token:
            options["tunnel_token"] = tunnel_token
        return options
    unifi = (cfg.get(CONF_UNIFI_HOST) or "").strip()
    if slug == ADDON_NAX_SENDER:
        options = {"api_token": pin, "crpc_host": processor, "crpc_pin": pin}
        # Only when an amplifier was actually given. Writing an empty list over
        # a house that already has amplifiers would silence it.
        nax_host = (cfg.get(CONF_NAX_HOST) or "").strip()
        if nax_host:
            options["amps"] = [{
                "host": nax_host,
                "user": "admin",
                "password": cfg.get(CONF_NAX_PASSWORD) or pin,
            }]
        return options
    if slug == ADDON_RAVA_BRIDGE:
        options = {"panel_password": pin, "api_token": pin,
                   "crpc": {"host": processor, "pin": pin}}
        # `set_options` merges into these rather than replacing them, so the
        # cameras and doorbells auto-detect has already found survive.
        protect_key = (cfg.get(CONF_PROTECT_API_KEY) or "").strip()
        if unifi and protect_key:
            options["protect"] = {"host": unifi, "api_key": protect_key}
        access_token = (cfg.get(CONF_ACCESS_TOKEN) or "").strip()
        if unifi and access_token:
            options["access"] = {"host": unifi, "token": access_token}
        return options
    if slug == ADDON_NAX_PROBE:
        return {}
    return {}


def ha_origin(hass: HomeAssistant) -> str:
    """Where cloudflared should send this house's traffic.

    🔴 Never a hardcoded port. Home Assistant does not always answer on 8123 -
    on HAOS 18.2 with Core 2026.9 the house answers on **80** and 8123 only
    redirects - and a tunnel pointed at the wrong port is a HEALTHY tunnel
    serving 502 Bad Gateway. That reads as a Cloudflare problem and is not one,
    which is exactly how long it took to find the first time.

    `hass.config.api` is what the HTTP server actually bound, so the PORT is
    taken from it whatever the version does.

    🔴 The HOST is always `homeassistant`, never `api.local_ip`. At 1151 East
    7th (2026-10-14) `local_ip` was 172.30.232.1 - HAOS's `docker0` bridge -
    and the tunnel pointed there: first every request arrived from
    172.30.232.x, outside the trusted 172.30.32.0/23, and got HA's bare 400;
    then cloudflared could not connect at all (`dial tcp 172.30.232.1:80: i/o
    timeout`). `homeassistant` is Core on the Supervisor's own network
    (172.30.32.1), which the cloudflared add-on is on, so the request comes
    from the trusted range - and unlike a LAN address it does not move when
    the box gets a new DHCP lease.
    """
    # Defensive all the way down: this runs in the middle of a commissioning
    # run, and an AttributeError here would abort the whole thing over a
    # cosmetic detail.
    api = getattr(getattr(hass, "config", None), "api", None)
    port = getattr(api, "port", None)
    if port:
        scheme = "https" if getattr(api, "use_ssl", False) else "http"
        return f"{scheme}://homeassistant:{port}"
    _LOGGER.warning("bav_house: Home Assistant did not report its own HTTP "
                    "port; falling back to %s", CF_HA_SERVICE)
    return CF_HA_SERVICE


async def run(hass: HomeAssistant, entry: ConfigEntry, progress: Progress,
              manual: bool = False) -> None:
    """Commission this house. Resumes; never raises.

    `manual` is a person running the action: it may retry what an automatic
    run will not, such as a trust Home Assistant reverted."""
    cfg = {**entry.data, **entry.options}
    done: list[str] = list(entry.options.get(STATE_STEPS) or [])
    session = async_get_clientsession(hass)
    sup = Supervisor(session)
    progress.running = True
    progress.started = time.time()
    progress.error = ""
    restarting = False

    try:
        if not sup.available:
            raise SupervisorError(
                "Home Assistant is not running under the Supervisor. Add-ons can "
                "only be installed on a Home Assistant OS or Supervised install."
            )

        tunnel_token = None
        troubles: list[str] = []
        wanted = list(cfg.get(CONF_ADDONS) or [])

        # 0. Home Assistant has to trust cloudflared, or the tunnel serves a
        # bare 400 on every path. First, because applying it restarts Home
        # Assistant: harmless now, costly once add-on images are being built.
        # Not just when this entry chose cloudflared: a tunnel made by hand, or
        # one from a commissioning without a Cloudflare token, 400s all the same.
        has_tunnel = bool(ADDON_CLOUDFLARED in wanted or cfg.get(CONF_HOSTNAME)
                          or entry.options.get(STATE_TUNNEL_ID)
                          or await _cloudflared_installed(sup))
        if has_tunnel:
            if await _proxy(hass, entry, progress, troubles, manual):
                # Home Assistant is going down, and this task with it. Still
                # here after RESTART_WAIT means it refused - a database upgrade
                # in progress, say - so report that and carry on rather than
                # sitting "running" with updates refused until somebody notices.
                restarting = True
                await asyncio.sleep(RESTART_WAIT)
                restarting = False
                detail = (f"asked Home Assistant to restart to apply it, and it "
                          f"had not after {RESTART_WAIT} s. Restart it, and "
                          f"commissioning carries on")
                troubles.append(f"the reverse-proxy trust ({detail})")
                progress.say(STEP_PROXY, "failed", detail)

        # 1. The tunnel, so that cloudflared has something to be told about.
        # A house that cannot have remote access today is still a house: a
        # refused Cloudflare token must not cost it its announcements and its
        # door station. cloudflared itself is dropped from the run, because a
        # connector with nothing to connect to just restarts forever.
        if ADDON_CLOUDFLARED in wanted and cfg.get(CONF_CF_API_TOKEN):
            try:
                tunnel_token, tunnel_id = await _cloudflare(hass, cfg, progress)
            except CloudflareError as err:
                troubles.append(f"the Cloudflare tunnel ({err})")
                progress.say(STEP_CLOUDFLARE, "failed", str(err))
                wanted = [s for s in wanted if s != ADDON_CLOUDFLARED]
            else:
                if tunnel_id:
                    done = _mark(done, STEP_CLOUDFLARE)
                    hass.config_entries.async_update_entry(
                        entry, options={**entry.options,
                                        STATE_TUNNEL_ID: tunnel_id,
                                        STATE_STEPS: done})

        # 2. The repositories. Adding one already present is a no-op, and one
        # that will not add - renamed, withdrawn, or never an add-on repository
        # in the first place - only costs the add-ons that were in it.
        progress.say(STEP_REPOS, "running")
        repo_failed = False
        for repo in (BAV_ADDON_REPO, CLOUDFLARED_ADDON_REPO):
            try:
                added = await sup.add_repository(repo)
            except SupervisorUnavailable:
                raise
            except SupervisorError as err:
                repo_failed = True
                troubles.append(f"the repository {repo} ({err})")
                progress.say(STEP_REPOS, "failed", f"{repo}: {err}")
                continue
            progress.say(STEP_REPOS, "ok",
                         f"{repo} {'added' if added else 'already present'}")
        # Without this the add-ons in a just-added repository are not yet in the
        # store listing, and resolving their slugs finds nothing. A reload that
        # is slow or refused - the Supervisor busy building an add-on left over
        # from a run a restart cut short - must not cost the whole run: the
        # add-ons already in the store still install, and one that is missing
        # says so by name below.
        try:
            await sup.reload_store()
        except SupervisorUnavailable:
            raise
        except SupervisorError as err:
            repo_failed = True
            troubles.append(f"the add-on store reload ({err})")
            progress.say(STEP_REPOS, "failed", f"store reload: {err}")
        if not repo_failed:
            done = _mark(done, STEP_REPOS)

        # 3. The add-ons themselves. One that fails must not take the others
        # with it: a house missing the probe is a house, a house that stopped
        # commissioning at the first error is a second visit.
        failures = []
        for slug in wanted:
            try:
                await _one_addon(sup, slug, cfg, tunnel_token, progress)
            except SupervisorUnavailable:
                raise
            except SupervisorError as err:
                failures.append(slug)
                troubles.append(f"the {slug} add-on ({err})")
                progress.say(STEP_ADDONS, "failed", f"{slug}: {err}")
        if not failures:
            done = _mark(done, STEP_ADDONS)

        # 4. Crestron Home, if its files are on disk and it is not already set up.
        if cfg.get(CONF_CRESTRON_HOST) and cfg.get(CONF_CRESTRON_TOKEN):
            refused = await _crestron(hass, cfg, progress)
            if refused:
                troubles.append(f"Crestron Home ({refused})")
            else:
                done = _mark(done, STEP_CRESTRON)

        # 5. From the outside, the way a person will reach it.
        if cfg.get(CONF_HOSTNAME):
            refused = await _public(session, cfg[CONF_HOSTNAME], progress)
            if refused:
                troubles.append(refused)

        hass.config_entries.async_update_entry(
            entry, options={**entry.options, STATE_STEPS: done})
        if troubles:
            progress.error = (
                f"Everything else is done, but {'; '.join(troubles)} did not. "
                f"Fix it and run the bav_house.provision action again - what "
                f"already worked is skipped.")
            progress.say("done", "failed", progress.error)
        else:
            progress.say("done", "ok", f"{len(wanted)} add-on(s)")
    except (SupervisorError, CloudflareError) as err:
        progress.error = str(err)
        progress.say(progress.step or "setup", "failed", str(err))
    except Exception as err:  # noqa: BLE001 - a half-commissioned house must still report
        progress.error = f"{type(err).__name__}: {err}"
        progress.say(progress.step or "setup", "failed", progress.error)
        _LOGGER.exception("bav_house: commissioning failed")
    finally:
        # Restarting to apply the proxy trust is not an ending: the sensor stays
        # "running" until Home Assistant goes down, and the next start carries on.
        progress.running = restarting
        progress.finished = 0.0 if restarting else time.time()


RESTART_WAIT = 180


async def _cloudflared_installed(sup: Supervisor) -> bool:
    """Is a cloudflared add-on on this box, whoever put it there?"""
    try:
        entry = await sup.find_in_store(ADDON_CLOUDFLARED)
        return bool(entry) and await sup.is_installed(entry["slug"])
    except SupervisorError:
        return False


async def _config_errors(hass: HomeAssistant) -> str | None:
    """Home Assistant's own pre-restart configuration check, run first.

    🔴 The restart service runs this check and refuses on errors, but called
    without blocking - the only safe way, since a blocking call waits on the
    shutdown that cancels this task - that refusal is lost. So ask first."""
    try:
        from homeassistant import config as conf_util  # noqa: PLC0415

        return await conf_util.async_check_ha_config_file(hass)
    except Exception:  # noqa: BLE001 - no answer: let the restart's own check decide
        return None


async def _proxy(hass: HomeAssistant, entry: ConfigEntry, progress: Progress,
                 troubles: list[str], manual: bool = False) -> bool:
    """Make Home Assistant trust cloudflared. True if it is restarting to apply it."""
    progress.say(STEP_PROXY, "running")
    staged = None
    try:
        what, detail, staged = await proxy.ensure(
            hass, entry.options.get(STATE_PROXY_STAGED), retry_reverted=manual)
    except Exception as err:  # noqa: BLE001 - the store is Home Assistant's, not ours
        _LOGGER.exception("bav_house: reverse-proxy trust")
        what, detail = proxy.BLOCKED, f"{type(err).__name__}: {err}"
    if what in (proxy.ALREADY, proxy.CONFIRMED):
        progress.say(STEP_PROXY, "ok", detail)
        return False
    if what == proxy.UNSUPPORTED:
        progress.say(STEP_PROXY, "ok", f"skipped: {detail}")
        return False
    if what == proxy.STAGED:
        last = float(entry.options.get(STATE_PROXY_RESTART) or 0)
        # Remember it as ours before anything else, so the next start confirms
        # it even if this one goes no further.
        hass.config_entries.async_update_entry(
            entry, options={**entry.options, STATE_PROXY_STAGED: staged})
        errors = await _config_errors(hass)
        if not manual and time.time() - last < PROXY_RESTART_GUARD:
            # Restarted for this a moment ago and it still is not in force:
            # restarting again unasked would only loop.
            detail = ("Home Assistant was restarted to apply it and came back "
                      "without it. Run commissioning again by hand, or set it in "
                      "Settings > System > Network > HTTP server: Trust "
                      "X-Forwarded-For on, trusted proxy 172.30.32.0/23, and "
                      "confirm it after the restart")
        elif errors:
            detail = (f"it is staged, but Home Assistant's configuration does not "
                      f"pass its own check, so it cannot be restarted to apply it: "
                      f"{errors}")
        else:
            hass.config_entries.async_update_entry(
                entry, options={**entry.options, STATE_PROXY_RESTART: time.time()})
            progress.say(STEP_PROXY, "running",
                         f"{detail}. Restarting Home Assistant now")
            try:
                await hass.services.async_call("homeassistant", "restart",
                                               blocking=False)
            except Exception as err:  # noqa: BLE001 - refused at the call itself
                detail = (f"could not restart Home Assistant to apply it ({err}); "
                          f"restart it, and commissioning carries on")
            else:
                return True
    troubles.append(f"the reverse-proxy trust ({detail})")
    progress.say(STEP_PROXY, "failed", detail)
    return False


# HA's reverse-proxy refusal: aiohttp's plain-text HTTPBadRequest, 16 bytes.
# Cloudflare's own errors are HTML pages, so this cannot be confused with them.
REFUSED_BODY = "400: Bad Request"
PUBLIC_URL = "https://{host}/manifest.json"
PUBLIC_TRIES = 3
PUBLIC_WAIT = 10


async def _public(session: aiohttp.ClientSession, hostname: str,
                  progress: Progress) -> str | None:
    """Fetch the house the way a person will. A refusal by Home Assistant is a
    trouble; anything else is only reported - a just-made DNS record or a
    cloudflared still connecting are not faults, and a Cloudflare Access login
    in front is a house working as intended."""
    url = PUBLIC_URL.format(host=hostname)
    status, text, err, location = 0, "", "", ""
    for attempt in range(PUBLIC_TRIES):
        try:
            async with session.get(
                    url, timeout=aiohttp.ClientTimeout(total=20),
                    allow_redirects=False,
                    headers={"User-Agent": "Mozilla/5.0 (bav_house)"}) as resp:
                status, text = resp.status, (await resp.text())[:200]
                location = resp.headers.get("Location", "")
                err = ""
        except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
            status, err = 0, f"{type(exc).__name__}: {exc}"
        if status == 400 and text.strip() == REFUSED_BODY:
            break
        if status and status < 500:
            break
        if attempt + 1 < PUBLIC_TRIES:
            await asyncio.sleep(PUBLIC_WAIT)
    if status == 400 and text.strip() == REFUSED_BODY:
        detail = (f"Home Assistant refused the tunnel: https://{hostname} answers "
                  f"400. Its HTTP settings do not trust cloudflared - Settings > "
                  f"System > Network > HTTP server: Trust X-Forwarded-For on, "
                  f"trusted proxy 172.30.32.0/23")
        progress.say(STEP_PUBLIC, "failed", detail)
        return detail
    if 300 <= status < 400 and "cloudflareaccess.com" in location:
        # Access answers for the origin, so this proves the tunnel and DNS but
        # says nothing about whether Home Assistant would refuse it.
        progress.say(STEP_PUBLIC, "ok",
                     f"https://{hostname} is behind Cloudflare Access - the tunnel "
                     f"answers, Home Assistant behind it not verified")
    elif status and status < 500:
        progress.say(STEP_PUBLIC, "ok", f"https://{hostname} answers ({status})")
    else:
        progress.say(STEP_PUBLIC, "ok",
                     f"https://{hostname} not answering yet "
                     f"({err or status}) - a new DNS record or a tunnel still "
                     f"connecting; check it again in a few minutes")
    return None


async def _cloudflare(hass: HomeAssistant, cfg: dict,
                      progress: Progress) -> tuple[str | None, str | None]:
    progress.say(STEP_CLOUDFLARE, "running")
    cf = Cloudflare(async_get_clientsession(hass), cfg[CONF_CF_API_TOKEN],
                    cfg[CONF_CF_ACCOUNT_ID])
    name = cfg.get(CONF_CF_TUNNEL_NAME) or ""
    hostname = cfg.get(CONF_HOSTNAME) or ""
    tunnel_id, token = await cf.ensure_tunnel(name)
    progress.say(STEP_CLOUDFLARE, "ok", f"tunnel {name} ({tunnel_id[:8]}…)")
    if hostname:
        kept = await cf.set_ingress(tunnel_id, hostname, ha_origin(hass))
        progress.say(STEP_CLOUDFLARE, "ok", f"{hostname} routed, {kept}")
        zone = cfg.get(CONF_CF_ZONE) or hostname.split(".", 1)[-1]
        what = await cf.ensure_cname(zone, hostname, tunnel_id)
        progress.say(STEP_CLOUDFLARE, "ok", f"{hostname} -> tunnel ({what})")
    return token, tunnel_id


async def _one_addon(sup: Supervisor, slug: str, cfg: dict,
                     tunnel_token: str | None, progress: Progress) -> None:
    progress.say(STEP_ADDONS, "running", slug)
    entry = await sup.find_in_store(slug)
    if not entry:
        raise SupervisorError(
            f"{slug} is not in the store - is its repository reachable?")
    full = entry["slug"]
    # An add-on that does not build for this machine is a fact about the
    # machine, not a fault to retry. Say which, because "install failed" on a
    # Pi and on an Intel NUC mean opposite things.
    arches = entry.get("arch") or []
    machine = await sup.arch()
    if arches and machine and machine not in arches:
        raise SupervisorError(
            f"{slug} builds for {', '.join(arches)}, and this machine is "
            f"{machine}")
    installed = await sup.install(full)
    progress.say(STEP_ADDONS, "ok",
                 f"{slug} {'installed' if installed else 'already installed'}")
    options = addon_options(slug, cfg, tunnel_token)
    if options:
        await sup.set_options(full, options)
        progress.say(STEP_ADDONS, "ok", f"{slug} configured")
    # The probe is a diagnostic that is run when it is wanted, not a service.
    if slug != ADDON_NAX_PROBE:
        await sup.set_boot(full, auto=True)
        await sup.start(full)
        progress.say(STEP_ADDONS, "ok", f"{slug} started")


async def _crestron(hass: HomeAssistant, cfg: dict, progress: Progress) -> str | None:
    """Set up the Crestron Home integration for this house's processor.
    Returns why not, or None when it is set up."""
    progress.say(STEP_CRESTRON, "running")
    host = cfg[CONF_CRESTRON_HOST]
    for existing in hass.config_entries.async_entries(CRESTRON_DOMAIN):
        if (existing.data or {}).get("host") == host:
            progress.say(STEP_CRESTRON, "ok", f"already set up for {host}")
            return None
    try:
        from homeassistant.loader import async_get_integration

        await async_get_integration(hass, CRESTRON_DOMAIN)
    except Exception:  # noqa: BLE001 - not installed is a legible outcome, not a crash
        why = (f"the {CRESTRON_DOMAIN} integration is not installed. Install the "
               f"BAV House Setup add-on, or restart Home Assistant if it was just "
               f"copied in.")
        progress.say(STEP_CRESTRON, "failed", why)
        return why
    result = await hass.config_entries.flow.async_init(
        CRESTRON_DOMAIN, context={"source": "user"},
        data={"host": host, "token": cfg[CONF_CRESTRON_TOKEN]},
    )
    if result.get("type") == "create_entry":
        progress.say(STEP_CRESTRON, "ok", f"connected to {host}")
        return None
    # A form coming back means it did not accept what it was given - almost
    # always the processor's REST token.
    errors = (result.get("errors") or {}).get("base") or result.get("reason") or ""
    why = f"the processor at {host} refused: {errors or 'check the API token'}"
    progress.say(STEP_CRESTRON, "failed", why)
    return why


def _mark(done: list[str], step: str) -> list[str]:
    return done if step in done else [*done, step]
