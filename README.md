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
| **Rava Bridge** (`ravabridge`) | 0.4.1 | [aarch64, amd64] | A SIP door station or phone rings the Crestron Home touch panels, with the door's picture, without touching the panels' Rava setup |
| **NAX AES67 Sender** (`naxaes67send`) | 0.4.2 | [aarch64] | AES67 announcements into a DM NAX - a always-on stream, spoken into on demand |
| **NAX AES67 Probe** (`naxaes67probe`) | 0.1.0 | [aarch64] | Read-only PTP/multicast feasibility probe for AES67 into the DM NAX |

Source of truth is the private `crestron_home` monorepo; this repository is
written by `Tools/publish_ha_addons.py` there and should not be edited by hand.
