"""The Supervisor's REST API, as much of it as installing an add-on needs.

Home Assistant Core runs with SUPERVISOR_TOKEN in its environment when it is
running under the Supervisor, and a custom integration is in that same process,
so it can drive the store directly - which is the whole reason this integration
can commission a house rather than describe how to.

Everything here is idempotent. Adding a repository that is already added, or
installing an add-on that is already installed, is a no-op and not an error:
commissioning gets re-run, and re-running it must be safe.
"""
from __future__ import annotations

import asyncio
import logging
import os

import aiohttp

_LOGGER = logging.getLogger(__name__)

# `http://supervisor` resolves on the Supervisor's own Docker network. Core is
# on that network, so unlike an add-on on the host's network it does not need
# the fixed-address fallback - but the fallback costs one failed connect and
# saves a house whose DNS is unhappy, so it is kept.
SUPERVISOR_URLS = ("http://supervisor", "http://172.30.32.2")

# Installing an add-on builds its image on the device. On a Raspberry Pi that is
# minutes, not seconds, and the Supervisor holds the request open for the whole
# build.
INSTALL_TIMEOUT = 30 * 60
DEFAULT_TIMEOUT = 60


class SupervisorError(Exception):
    """The Supervisor was reached and said no."""


class SupervisorUnavailable(SupervisorError):
    """The Supervisor could not be reached at all - or there is no token."""


def token() -> str | None:
    """The Supervisor token Core was started with, if it was."""
    return os.environ.get("SUPERVISOR_TOKEN") or os.environ.get("HASSIO_TOKEN")


class Supervisor:
    """A thin client bound to one aiohttp session."""

    def __init__(self, session: aiohttp.ClientSession) -> None:
        self._session = session
        self._token = token()
        # Remembered once found, so only the first call pays for a bad address.
        self._base: str | None = None

    @property
    def available(self) -> bool:
        return bool(self._token)

    async def _call(self, method: str, path: str, body: dict | None = None,
                    timeout: int = DEFAULT_TIMEOUT) -> dict:
        if not self._token:
            raise SupervisorUnavailable(
                "Home Assistant is not running under the Supervisor, so add-ons "
                "cannot be installed from here"
            )
        bases = [self._base] if self._base else []
        bases += [u for u in SUPERVISOR_URLS if u != self._base]
        last: Exception | None = None
        for base in bases:
            try:
                async with self._session.request(
                    method, base + path, json=body,
                    headers={"Authorization": f"Bearer {self._token}"},
                    timeout=aiohttp.ClientTimeout(total=timeout),
                ) as resp:
                    payload = await _read_json(resp)
                    if resp.status >= 400:
                        # Reached it; it refused. Another address would refuse
                        # identically, so stop rather than retrying the refusal.
                        self._base = base
                        raise SupervisorError(
                            f"{method} {path}: {resp.status} "
                            f"{payload.get('message') or payload}"
                        )
                    self._base = base
                    return payload
            except (aiohttp.ClientError, asyncio.TimeoutError) as err:
                last = err
        raise SupervisorUnavailable(f"could not reach the Supervisor: {last!r}")

    # -- store -----------------------------------------------------------

    async def add_repository(self, url: str) -> bool:
        """Add an add-on repository. True if it was actually added."""
        existing = await self.repositories()
        if any(_same_repo(url, r) for r in existing):
            return False
        await self._call("POST", "/store/repositories", {"repository": url},
                         timeout=180)
        return True

    async def repositories(self) -> list[str]:
        data = (await self._call("GET", "/store")).get("data") or {}
        return [
            r.get("source") or r.get("slug") or ""
            for r in (data.get("repositories") or [])
        ]

    async def reload_store(self) -> None:
        """Re-read every repository. Without this a freshly added repository's
        add-ons, and a bumped version of an installed one, are not visible."""
        await self._call("POST", "/store/reload", timeout=300)

    async def store_addons(self) -> list[dict]:
        data = (await self._call("GET", "/store")).get("data") or {}
        return data.get("addons") or []

    async def arch(self) -> str | None:
        """What this machine builds for - `aarch64` on a Pi, `amd64` on a NUC."""
        try:
            data = (await self._call("GET", "/info")).get("data") or {}
        except SupervisorError:
            return None
        return data.get("arch")

    async def find_in_store(self, bare_slug: str) -> dict | None:
        """The store's entry for an add-on named `bare_slug`.

        A repository add-on is `<repository hash>_<slug>` - the hash differs per
        repository URL, so it cannot be written down, only looked up. A local
        add-on is `local_<slug>`. Prefer a repository copy over a local one:
        a stray local install is the failure mode that fights the real one for
        a port (it has happened, on the NAX sender, at a client's house).
        """
        fallback = None
        for addon in await self.store_addons():
            slug = addon.get("slug") or ""
            if slug == bare_slug or slug.endswith(f"_{bare_slug}"):
                if not slug.startswith("local_"):
                    return addon
                fallback = fallback or addon
        return fallback

    async def resolve_slug(self, bare_slug: str) -> str | None:
        """Just the slug, for callers that do not need the rest of the entry."""
        entry = await self.find_in_store(bare_slug)
        return entry.get("slug") if entry else None

    # -- add-ons ---------------------------------------------------------

    async def info(self, slug: str) -> dict:
        return (await self._call("GET", f"/addons/{slug}/info")).get("data") or {}

    async def is_installed(self, slug: str) -> bool:
        try:
            return bool((await self.info(slug)).get("version"))
        except SupervisorError:
            return False

    async def install(self, slug: str) -> bool:
        """Install it if it is not already. True if this call did the install."""
        if await self.is_installed(slug):
            return False
        await self._call("POST", f"/store/addons/{slug}/install",
                         timeout=INSTALL_TIMEOUT)
        return True

    async def update(self, slug: str) -> None:
        await self._call("POST", f"/store/addons/{slug}/update",
                         timeout=INSTALL_TIMEOUT)

    async def set_options(self, slug: str, options: dict) -> None:
        """Merge `options` into the add-on's current options.

        Merge, never replace: a house that has been tuned by hand - zone
        volumes, a panel somebody typed in - must not lose that because it was
        re-commissioned. Only the keys given here are touched.
        """
        current = (await self.info(slug)).get("options") or {}
        merged = dict(current)
        merged.update(options)
        if merged == current:
            return
        await self._call("POST", f"/addons/{slug}/options", {"options": merged})

    async def set_boot(self, slug: str, auto: bool = True) -> None:
        await self._call("POST", f"/addons/{slug}/options",
                         {"boot": "auto" if auto else "manual"})

    async def start(self, slug: str) -> None:
        """Start it, unless it is already started."""
        if (await self.info(slug)).get("state") == "started":
            return
        await self._call("POST", f"/addons/{slug}/start", timeout=300)

    async def restart(self, slug: str) -> None:
        await self._call("POST", f"/addons/{slug}/restart", timeout=300)


async def _read_json(resp: aiohttp.ClientResponse) -> dict:
    try:
        payload = await resp.json(content_type=None)
    except ValueError:
        payload = None
    return payload if isinstance(payload, dict) else {}


def _same_repo(a: str, b: str) -> bool:
    """Repository URLs differ harmlessly - a trailing slash, a `.git`, case."""
    return _norm_repo(a) == _norm_repo(b)


def _norm_repo(url: str) -> str:
    url = (url or "").strip().lower().rstrip("/")
    return url[:-4] if url.endswith(".git") else url
