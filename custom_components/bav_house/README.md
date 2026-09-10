# BAV House Setup (integration, `bav_house`)

Commissions a house's Home Assistant from one form: the Cloudflare tunnel, the
add-ons, and Crestron Home. Then it keeps the custom integrations up to date,
which Home Assistant will not do on its own.

Domain `bav_house`. Lives at `/config/custom_components/bav_house`.

## What it does, in order

1. **The tunnel.** Creates a Cloudflare Tunnel with the name you give it, routes
   it at Home Assistant, and points `<site>.bav.homes` at it. Returns the
   connector token.
2. **The repositories.** Adds `jonahshem/ha-addons` and
   `homeassistant-apps/app-cloudflared` to the add-on store, then reloads it -
   without the reload a just-added repository's add-ons are not in the listing
   and resolving their slugs finds nothing.
3. **The add-ons.** For each one chosen: install, write this house's settings
   into its options, set it to start on boot, start it.
4. **Crestron Home.** Creates the `crestron_home` config entry against the
   processor, using the address and token already checked on the form.

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
  "device_pin": "2129918115"
}
```

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
no other way to swap it.

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
