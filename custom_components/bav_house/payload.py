"""Getting a custom integration onto the box, and keeping it current.

Home Assistant has no mechanism for updating a custom integration - the store
only knows about add-ons - so this is that mechanism, kept as small as it can
be: a manifest in the public repository lists each integration, its version and
its files; a house compares that to the `version` in the manifest.json already
on disk, and an `update` entity offers the difference like any other update.

One source for everything: the same repository the add-ons come from.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil

import aiohttp

_LOGGER = logging.getLogger(__name__)

RAW = "https://raw.githubusercontent.com/jonahshem/ha-addons/main"
MANIFEST_URL = f"{RAW}/custom_components/versions.json"
TIMEOUT = aiohttp.ClientTimeout(total=60)

# The integrations this one looks after, including itself: an update to the
# updater has to arrive the same way as everything else, or it never arrives.
MANAGED = ("crestron_home", "bav_house")


class PayloadError(Exception):
    """The repository could not be read, or gave something unusable."""


async def fetch_manifest(session: aiohttp.ClientSession) -> dict:
    try:
        async with session.get(MANIFEST_URL, timeout=TIMEOUT) as resp:
            if resp.status != 200:
                raise PayloadError(f"{MANIFEST_URL}: HTTP {resp.status}")
            data = await resp.json(content_type=None)
    except (aiohttp.ClientError, asyncio.TimeoutError, ValueError) as err:
        raise PayloadError(f"{MANIFEST_URL}: {err!r}") from err
    if not isinstance(data, dict) or not isinstance(data.get("integrations"), dict):
        raise PayloadError("versions.json is not the shape this expects")
    return data


def installed_version(config_dir: str, domain: str) -> str | None:
    """The version in the manifest.json already on disk, or None if absent."""
    path = os.path.join(config_dir, "custom_components", domain, "manifest.json")
    try:
        with open(path, encoding="utf-8") as handle:
            return (json.load(handle) or {}).get("version")
    except (OSError, ValueError):
        return None


async def install(session: aiohttp.ClientSession, manifest: dict, domain: str,
                  config_dir: str) -> str:
    """Download every file of `domain` and write them in. Returns the version.

    Downloaded in full before anything is written: a half-written integration is
    one Home Assistant refuses to start, and a house that will not start is a
    call-out. Nothing is deleted that the new version does not replace, so a
    file dropped between releases lingers harmlessly rather than risking the
    directory being emptied by a bad manifest.
    """
    entry = (manifest.get("integrations") or {}).get(domain)
    if not entry:
        raise PayloadError(f"{domain} is not in versions.json")
    base = manifest.get("base") or f"{RAW}/custom_components"
    files = entry.get("files") or []
    if not files:
        raise PayloadError(f"{domain} lists no files")

    blobs: dict[str, bytes] = {}
    for name in files:
        url = f"{base}/{domain}/{name}"
        try:
            async with session.get(url, timeout=TIMEOUT) as resp:
                if resp.status != 200:
                    raise PayloadError(f"{url}: HTTP {resp.status}")
                blobs[name] = await resp.read()
        except (aiohttp.ClientError, asyncio.TimeoutError) as err:
            raise PayloadError(f"{url}: {err!r}") from err

    target = os.path.join(config_dir, "custom_components", domain)
    _write(target, blobs)
    version = entry.get("version") or "0"
    _LOGGER.info("bav_house: wrote %s %s (%d files)", domain, version, len(blobs))
    return version


def _write(target: str, blobs: dict[str, bytes]) -> None:
    staging = target + ".new"
    shutil.rmtree(staging, ignore_errors=True)
    os.makedirs(staging, exist_ok=True)
    try:
        for name, blob in blobs.items():
            path = os.path.join(staging, name)
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "wb") as handle:
                handle.write(blob)
        os.makedirs(target, exist_ok=True)
        # Moved file by file rather than by swapping directories: the old
        # directory is what is currently imported, and on some filesystems
        # replacing it out from under a running process is not clean.
        for name in blobs:
            src, dst = os.path.join(staging, name), os.path.join(target, name)
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            os.replace(src, dst)
    finally:
        shutil.rmtree(staging, ignore_errors=True)
