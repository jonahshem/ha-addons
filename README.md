# BAV Home Assistant Apps

Add-ons for the houses this dealer looks after. Add this repository once, in
**Settings → Add-ons → Add-on Store → ⋮ → Repositories**:

```
https://github.com/jonahshem/ha-addons
```

Each add-on is built on the device from its Dockerfile when installed, so the
first install takes a couple of minutes on a Raspberry Pi. Updates appear in the
store when a version changes here.

| Add-on | Version | Arch | What it does |
|---|---|---|---|
| **BAV House Setup** (`bavsetup`) | 1.0.0 | [aarch64, amd64] | Puts the house-commissioning integration on this box and starts it - install this first on a new house |
| **Rava Bridge** (`ravabridge`) | 0.11.0 | [aarch64, amd64] | A SIP door station or phone rings the Crestron Home touch panels, with the door's picture, without touching the panels' Rava setup |
| **NAX AES67 Sender** (`naxaes67send`) | 0.8.0 | [aarch64, amd64] | AES67 announcements into a DM NAX - a always-on stream, spoken into on demand |
| **NAX AES67 Probe** (`naxaes67probe`) | 0.2.0 | [aarch64, amd64] | Read-only PTP/multicast feasibility probe for AES67 into the DM NAX |

## Custom integrations

Home Assistant has no add-on store for custom integrations, so these are
published here as plain files and installed by the **BAV House Setup** add-on
(or by an imaged box). Once installed, `bav_house` offers each new version as
an ordinary update in **Settings > Updates**.

| Integration | Version | Files |
|---|---|---|
| `bav_house` | 1.0.1 | 15 |
| `crestron_home` | 1.1.0 | 19 |

Source of truth is the private `crestron_home` monorepo; this repository is
written by `Tools/publish_ha_addons.py` there and should not be edited by hand.
