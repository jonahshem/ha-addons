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

import logging
import time
from dataclasses import dataclass, field

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .cloudflare import Cloudflare, CloudflareError
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
    CONF_DEVICE_PIN,
    CONF_HOSTNAME,
    CRESTRON_DOMAIN,
    DEFAULT_DEVICE_PIN,
    STATE_STEPS,
    STATE_TUNNEL_ID,
    STEP_ADDONS,
    STEP_CLOUDFLARE,
    STEP_CRESTRON,
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
    if slug == ADDON_NAX_SENDER:
        return {"api_token": pin, "crpc_host": processor, "crpc_pin": pin}
    if slug == ADDON_RAVA_BRIDGE:
        return {"panel_password": pin, "api_token": pin,
                "crpc": {"host": processor, "pin": pin}}
    if slug == ADDON_NAX_PROBE:
        return {}
    return {}


async def run(hass: HomeAssistant, entry: ConfigEntry, progress: Progress) -> None:
    """Commission this house. Resumes; never raises."""
    cfg = {**entry.data, **entry.options}
    done: list[str] = list(entry.options.get(STATE_STEPS) or [])
    session = async_get_clientsession(hass)
    sup = Supervisor(session)
    progress.running = True
    progress.started = time.time()
    progress.error = ""

    try:
        if not sup.available:
            raise SupervisorError(
                "Home Assistant is not running under the Supervisor. Add-ons can "
                "only be installed on a Home Assistant OS or Supervised install."
            )

        tunnel_token = None
        troubles: list[str] = []
        wanted = list(cfg.get(CONF_ADDONS) or [])

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
        # store listing, and resolving their slugs finds nothing.
        await sup.reload_store()
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
            await _crestron(hass, cfg, progress)
            done = _mark(done, STEP_CRESTRON)

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
        progress.running = False
        progress.finished = time.time()


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
        kept = await cf.set_ingress(tunnel_id, hostname)
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


async def _crestron(hass: HomeAssistant, cfg: dict, progress: Progress) -> None:
    """Set up the Crestron Home integration for this house's processor."""
    progress.say(STEP_CRESTRON, "running")
    host = cfg[CONF_CRESTRON_HOST]
    for existing in hass.config_entries.async_entries(CRESTRON_DOMAIN):
        if (existing.data or {}).get("host") == host:
            progress.say(STEP_CRESTRON, "ok", f"already set up for {host}")
            return
    try:
        from homeassistant.loader import async_get_integration

        await async_get_integration(hass, CRESTRON_DOMAIN)
    except Exception:  # noqa: BLE001 - not installed is a legible outcome, not a crash
        progress.say(
            STEP_CRESTRON, "failed",
            f"the {CRESTRON_DOMAIN} integration is not installed. Install the "
            f"BAV House Setup add-on, or restart Home Assistant if it was just "
            f"copied in.")
        return
    result = await hass.config_entries.flow.async_init(
        CRESTRON_DOMAIN, context={"source": "user"},
        data={"host": host, "token": cfg[CONF_CRESTRON_TOKEN]},
    )
    if result.get("type") == "create_entry":
        progress.say(STEP_CRESTRON, "ok", f"connected to {host}")
        return
    # A form coming back means it did not accept what it was given - almost
    # always the processor's REST token.
    errors = (result.get("errors") or {}).get("base") or result.get("reason") or ""
    progress.say(STEP_CRESTRON, "failed",
                 f"the processor at {host} refused: {errors or 'check the API token'}")


def _mark(done: list[str], step: str) -> list[str]:
    return done if step in done else [*done, step]
