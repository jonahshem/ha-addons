# BAV House Setup (integration, `bav_house`)

Commissions a house's Home Assistant from one form: the Cloudflare tunnel, the
add-ons, and Crestron Home. Then it keeps the custom integrations up to date,
which Home Assistant will not do on its own.

Domain `bav_house`. Lives at `/config/custom_components/bav_house`.

## What it does, in order

0. **The reverse-proxy trust** (when cloudflared is wanted). Home Assistant
   refuses a proxied request unless its HTTP settings trust the proxy, so
   without this a healthy tunnel serves a bare `400: Bad Request` on every path
   (1151 East 7th, 2026-10-14). 🔴 From Core 2026.8 those settings live in a
   store behind Settings > System > Network, not in `configuration.yaml` - an
   `http:` block added after the first boot is ignored. So `proxy.py` does what
   that settings page does: stages `use_x_forwarded_for` + `172.30.32.0/23` as
   the *pending* config, copying everything else (the port above all) from the
   confirmed one, and restarts. Home Assistant starts on it as a five-minute
   trial; commissioning runs again on that start, sees its own trial running
   and **confirms it**, then carries on. 🔴 Only its *own* trial: the pending
   config's `created_at` is recorded on the entry when it is staged, and
   nothing else is ever confirmed - a person's port or SSL change that happens
   to keep the trust stays theirs, with Home Assistant's five-minute revert
   still armed. Somebody else's staged change is left alone; their failed one
   (an old seeded `http:` block Home Assistant migrated onto 8123 and reverted
   is the usual one) is replaced. Its own reverted trial is retried only when a
   person runs the action, and it never restarts twice for this within 15
   minutes unasked. It runs Home Assistant's configuration check before asking
   for the restart, and if Home Assistant is still up three minutes after
   asking, it says so and carries on. First in the run because the restart is
   harmless before add-ons are being built and costly during. A house that
   trusts only the docs' `172.30.33.0/24` gets `/23` added - one extra restart.
1. **The tunnel.** Creates a Cloudflare Tunnel with the name you give it, routes
   it at Home Assistant (`http://homeassistant:<the port HA bound>` - never the
   IP HA reports for itself, which at 1151 East 7th was HAOS's `docker0` bridge
   and left the tunnel first 400ing, then timing out), and points `<site>.bav.homes` at it. Returns the
   connector token.
2. **The repositories.** Adds `jonahshem/ha-addons` and
   `homeassistant-apps/app-cloudflared` to the add-on store, then reloads it -
   without the reload a just-added repository's add-ons are not in the listing
   and resolving their slugs finds nothing.
3. **The add-ons.** For each one chosen: install, write this house's settings
   into its options, set it to start on boot, start it.
4. **Crestron Home.** Creates the `crestron_home` config entry against the
   processor, using the address and token already checked on the form.
5. **From the outside.** Fetches `https://<hostname>/manifest.json` the way a
   person will. Home Assistant's own refusal (`400: Bad Request`, plain text)
   fails the run with the setting to change; a tunnel still connecting or a
   brand-new DNS record is only noted, and a Cloudflare Access login in front
   counts as working.

A slow or refused add-on store reload costs only itself: the add-ons already in
the store still install, and one that is missing says so by name. A Supervisor
call that times out is never re-sent to the Supervisor's second address - it is
the same Supervisor, and the POST may be running there already.

Every step records itself on the config entry, and **nothing is done twice**:
adding a repository that is there, installing an add-on that is installed, or
creating a tunnel that exists are all no-ops. So `action: bav_house.provision`
is the thing to run after fixing a wrong password - it skips what worked and
retries what did not.

An add-on that fails does not stop the others. A house missing the probe is a
house; a house that stopped commissioning at the first error is a second visit.

## Why the tunnel is made here rather than by the add-on

The cloudflared add-on takes either `tunnel_name` + `external_hostname`, or a
`tunnel_token`. The first prints a URL in the add-on log that **somebody has to
open in a browser and authorise**. That is fine once and unacceptable across a
fleet, so this makes the tunnel through Cloudflare's API first and hands the
add-on a token. Nobody opens a browser.

The API token needs **Cloudflare Tunnel: Edit** on the account and **DNS: Edit**
on the zone.

## Fleet credentials, and where they are not

The Cloudflare token, account and zone are the same for every house, so the form
should not ask for them. They are read from **`/config/.bav_fleet.json`**, which
the image seeder writes:

```json
{
  "cf_api_token": "…",
  "cf_account_id": "…",
  "zone": "bav.homes",
  "device_pin": "2129918115",
  "fish_api_key": "…"
}
```

`fish_api_key` is not this integration's: the NAX AES67 Sender add-on reads it
(the add-on maps the config folder), so every house speaks announcements with
the same Fish Audio account without the key being published. Its page can also
write it into this file on a house that was not seeded.

The form shows them as already answered, with a **Change the Cloudflare account**
tick that reveals the fields.

🔴 **They are deliberately not defaults in the add-on repository.** That
repository is public. The device PIN already is a public default and that is
survivable - it is a PIN on a house LAN. A Cloudflare token with tunnel and DNS
write is not: anyone could stand up a hostname in the zone. A house set up by
hand, rather than from a seeded image, has no fleet file and is asked once.

## Updates

Home Assistant gives an add-on an Update button for free and a custom
integration nothing at all, so a house runs whatever was copied in at
commissioning until somebody notices. This closes that.

`custom_components/versions.json` in the public repository lists each managed
integration and its files. The integration compares that to the `version` in the
`manifest.json` on disk and offers the difference in **Settings → Updates**, for
`crestron_home` and for itself. Installing writes the new files and restarts
Home Assistant, because a custom integration is imported at start and there is
no other way to swap it. 🔴 Installing is refused while commissioning is
running: the restart would cut the run short in the middle of whatever the
Supervisor is building for it (1151 East 7th, 2026-10-14).

One source for everything: the same repository the add-ons come from.

## Entities

| Entity | What it is for |
|---|---|
| `sensor.<house>_commissioning` | `not started` / `running` / `ok` / `failed`, with every line of the run on its attributes. Commissioning takes minutes on a Pi, most of it building add-on images, and somebody in a plant room needs to know whether it is working without SSH. |
| `update.<house>_crestron_home` | A new version of the Crestron Home integration. Unavailable if it is not installed here - absent is not "up to date". |
| `update.<house>_bav_house_setup` | A new version of this. An update to the updater has to arrive the same way as everything else, or it never arrives. |

## Getting it onto a box

**From an image** - `Tools/seed_haos_image.py` writes it into a Home Assistant OS
image's data partition before it ever boots, along with the fleet file and a
`bav_house:` line in `configuration.yaml`. Flash, boot, and the house offers to
commission itself.

**By hand** - install the **BAV House Setup add-on**, which does the same
writing from inside a running box. Same files, same code afterwards.
