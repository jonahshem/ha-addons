# BAV House Setup

Install this first on a new house. It is the smallest thing that can exist,
because everything it does is over in about thirty seconds and then it is done.

## Why it exists

Home Assistant will install an **add-on** from a repository URL - one paste into
Settings → Add-ons → Add-on Store → ⋮ → Repositories. It has no equivalent for a
**custom integration**: there is no store for those, so the only ways onto a box
are Samba, SSH, the File editor add-on, or HACS. None of them is a thing to talk
somebody through on the phone.

So this add-on is the paste. It writes the commissioning integration into
`/config/custom_components`, adds one line to `configuration.yaml` so the
integration loads on every boot, and restarts Home Assistant. That is all.

Everything that actually commissions the house - the credentials form, the
Cloudflare tunnel, installing and configuring the other add-ons, setting up
Crestron Home - is in that integration, not here. A house built from a **seeded
image** already has the integration and never installs this add-on at all, and
running the same code both ways is the point.

## Installing

Add the repository once:

```
https://github.com/jonahshem/ha-addons
```

Install **BAV House Setup** and start it. It runs on start, so there is nothing
to press. When Home Assistant comes back, go to **Settings → Devices & services**
and set up **BAV House Setup** - the integration, not this.

You can then stop this add-on. Leaving it installed costs nothing and makes
re-running the file install a click away.

## Options

| Option | Default | What it does |
|---|---|---|
| `run_on_start` | `true` | Do the job when the add-on starts, rather than waiting for the button on its page. |
| `start_on_boot` | `true` | Add `bav_house:` to `configuration.yaml`, so a house that has not been commissioned says so on every boot. The file is backed up to `configuration.yaml.bavsetup.bak` first, and the line is never added twice. |
| `restart_after` | `true` | Restart Home Assistant when the files are in place. A custom integration is imported at start and no other way, so with this off nothing happens until the next restart anyway. |

## Where the files come from

`https://github.com/jonahshem/ha-addons` - the same public repository this add-on
came from. `custom_components/versions.json` there lists each integration, its
version and its files; this fetches them and writes them whole, never partially:
a half-written integration is a Home Assistant that will not start, and a house
that will not start is a call-out.

After the first install, updates do **not** come from here. The `bav_house`
integration checks the same manifest and offers each new version as an ordinary
update in **Settings → Updates**, including updates to itself.

## Two things that will bite

🔴 **`/config` is not Home Assistant's config directory.** With
`map: homeassistant_config`, the Supervisor mounts Home Assistant's `/config` at
**`/homeassistant`** inside the container, and `/config` is this add-on's own
private folder. Writing `custom_components` to `/config` succeeds, is invisible
to Home Assistant, and looks exactly like a bug in the integration. `main.py`
looks for `configuration.yaml` to decide which it is on.

🔴 **`run.sh` must start `#!/usr/bin/with-contenv bash`.** `SUPERVISOR_TOKEN`
reaches the container through s6's environment directory; a script started
outside it never sees the variable. The file install then works and the restart
afterwards fails, which is a confusing way round to fail.
