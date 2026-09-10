"""Names and defaults shared across the integration.

The defaults exist so that commissioning a house is a matter of confirming a
form rather than filling one in. Every one of them can be overridden per site;
none of them is a secret that matters on its own (the PIN is a device PIN on a
house LAN, already public in the add-on repository's own config.yaml).
"""

DOMAIN = "bav_house"

# The dealer's common device PIN. Crestron Home sets it on every device it
# commissions, so it is what the NAX amplifiers, the touch panels and the
# processor's own UI all answer to, and what the add-ons already default to.
DEFAULT_DEVICE_PIN = "2129918115"

# The public add-on repository every house installs from, and the community
# repository cloudflared comes from. Both are added to the store at provision
# time; adding one that is already there is not an error.
BAV_ADDON_REPO = "https://github.com/jonahshem/ha-addons"
# 🔴 NOT `homeassistant-apps/app-cloudflared` - that is where the add-on's source
# and its DOCS.md live, and it is not an add-on repository. The Supervisor
# rejects it with "is not a valid app repository" (400), which the docs give no
# hint of. An add-on repository is the one with `repository.yaml` at its root.
CLOUDFLARED_ADDON_REPO = "https://github.com/brenner-tobias/ha-addons"

# Add-on slugs, as the Supervisor knows them once a repository is added. The
# Supervisor prefixes a repository add-on with a hash of the repository URL, so
# these bare slugs are matched against the store listing rather than used
# directly - see `supervisor.resolve_slug`.
ADDON_NAX_SENDER = "naxaes67send"
ADDON_RAVA_BRIDGE = "ravabridge"
ADDON_NAX_PROBE = "naxaes67probe"
ADDON_CLOUDFLARED = "cloudflared"

# What the setup form offers, in the order it offers it. `default` is whether
# the box is ticked for a new house: the probe is a diagnostic, not part of a
# normal commission.
ADDON_CHOICES = (
    (ADDON_CLOUDFLARED, "Cloudflare Tunnel (remote access)", True),
    (ADDON_NAX_SENDER, "NAX AES67 Sender (announcements)", True),
    (ADDON_RAVA_BRIDGE, "Rava Bridge (door stations, intercom)", True),
    (ADDON_NAX_PROBE, "NAX AES67 Probe (diagnostic, off by default)", False),
)

# The Crestron Home integration this one installs and configures on the house's
# behalf. Its domain is not ours, so it is named rather than imported.
CRESTRON_DOMAIN = "crestron_home"

# Where the image seeder leaves the credentials that are the same for every
# house in the fleet, so that a seeded box asks for none of them. Absent on a
# box set up by hand, which is why every field it supplies is still editable.
FLEET_DEFAULTS_PATH = ".bav_fleet.json"

# Config entry data keys.
CONF_SITE_NAME = "site_name"
CONF_SITE_SLUG = "site_slug"
CONF_HOSTNAME = "hostname"
CONF_DEVICE_PIN = "device_pin"
CONF_ADDONS = "addons"
CONF_CRESTRON_HOST = "crestron_host"
CONF_CRESTRON_TOKEN = "crestron_token"
CONF_CF_API_TOKEN = "cf_api_token"
CONF_CF_ACCOUNT_ID = "cf_account_id"
CONF_CF_ZONE = "cf_zone"
CONF_CF_TUNNEL_NAME = "cf_tunnel_name"
CONF_CHANGE_CF = "change_cloudflare"

# Provisioning state, persisted on the config entry so that a run interrupted by
# a restart resumes rather than starting over.
STATE_STEPS = "steps_done"
STATE_LAST_ERROR = "last_error"
STATE_TUNNEL_ID = "tunnel_id"

STEP_CLOUDFLARE = "cloudflare"
STEP_REPOS = "repositories"
STEP_ADDONS = "addons"
STEP_CRESTRON = "crestron"
