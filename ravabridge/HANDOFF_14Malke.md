> **SUPERSEDED 2026-09-09.** The current state of 14 Malke is in
> `NaxAes67Sender/HANDOFF_14Malke_Announcements.md`. Read that first. What changed since this was written:
>
> - **A second real panel is installed** (Social Room `192.168.0.17`, device 99032). Pages now send; five clean
>   pages in a row were relayed to the speakers. The "pending second-panel test" below is done.
> - **The fake "NAX Speakers" panel was REMOVED from the processor on 2026-09-09** (live file edit + `REBOOT`).
>   **Do NOT run `python Tools/mc4r_fake_panel.py restore`** - its 2026-09-07 originals predate the second
>   panel and would erase it. Removal backups: `Backups/14Malke_MC4R_2026-09-09-before-removal/` and
>   `/user/Backups/intercom-2026-09-09-before-removal` on the MC4-R.
> - **Rava Bridge 0.5.1 no longer registers with the processor** (the `crpc` / `intercom_devices` path is off).
>   The relay posts `/live?zones=auto`; the fixed Den + Kitchen zones below are gone.
> - **Sender 0.7.1**: stream moved to `239.69.4.5` (the switch would not re-forward the old group), speaker
>   groups + per-zone volume on `/ui`, music resumed after a page via its own CRPC session.
> - The relay's HTTP timeout was raised from 10 s to 90 s: five-zone pages timed out before the sender replied.
>
> Everything below is history as of 2026-09-08 and is kept for the processor-edit record and the boundary finding.

# 14 Malke - what was done, and the state it is in (2026-09-07 / 08)

Handoff for another session. Everything below was measured live; nothing is
assumed. Test house: credentials are deliberately plain.

## The house

| What | Where | Notes |
|---|---|---|
| Crestron Home processor | MC4-R `192.168.0.50` | SSH/SFTP `admin` / `2129918115`. Nightly reboot ~04:01. No `PROGRESET`; use `REBOOT`. |
| Touch panel | TSW-770R `192.168.0.225` (Kitchen), ext `2053066235` | Rava **PEER** mode - must stay that way (server mode breaks native intercom). The ONLY real panel. |
| NAX amplifier | DM-NAX-8ZSA `192.168.0.51` | 8 zones: Zone1 Den, Zone2 Dining Room, Zone3 Living Room, Zone4 Deck, Zone5 Kitchen, Zone6 Social Room, Zone7 Master Bed, Zone8. Login `chdevice` / `2129918115`. |
| Home Assistant | HAOS `192.168.0.97` | Our add-ons run here (host network). SSH `root@192.168.0.97` with key `~/.ssh/ravabridge_ha`. |
| UniFi Access reader | door "Side Door", door_id `3320c8a7-c4e7-454c-bce0-7997c008cffd`, via controller `192.168.0.1` | Third-party SIP (mjsip) registering to the bridge as user `sidedoor`. |
| Josh.ai | `192.168.0.138` | Not ours; explains the NAX "Josh VoiceLink" input. |

VPN into the house is required for any of this.

## What is installed on the HA box

All from the public add-on repo **github.com/jonahshem/ha-addons** (publish with
`Tools/publish_ha_addons.py`; a version bump + `ha addons update` is a deploy).
All auto-start; full power-failure recovery was verified on 2026-09-07.

| Add-on | Version | Ports | Role |
|---|---|---|---|
| Rava Bridge (`60ba3119_ravabridge`) | **0.5.0** | SIP 5060, API 8098 | Door station to panel bridge, page listener, page to NAX relay, CRPC intercom endpoint |
| NAX AES67 Sender (`60ba3119_naxaes67send`) | **0.5.1** | API 8099, AES67 239.69.4.4:5004 | Always-on AES67 stream into the NAX; `/announce` (clips) and the new `/live` (streamed page) |
| NAX AES67 Probe | 0.1.0 | - | stopped, diagnostics only |

API token for both: `2129918115`. Bridge status page: `http://192.168.0.97:8098/`.

### Rava Bridge options as they are right now

```json
"panels":  [{"name":"Panel 225","host":"192.168.0.225","ext":"2053066235","groups":["1st floor"]}],
"doors":   [{"name":"Side Door","user":"sidedoor","password":"2129918115","ring":["all"],"door_id":"3320c8a7-c4e7-454c-bce0-7997c008cffd"}],
"access":  {"host":"192.168.0.1","token":"<UniFi Access API token - read it from the bridge options on the box>","door_id":"3320c8a7-c4e7-454c-bce0-7997c008cffd"},
"page_listen": true, "page_group":"227.1.1.1", "page_port":1234,
"page_relay": {"enabled":true,"url":"http://192.168.0.97:8099","token":"2129918115",
               "zones":["192.168.0.51:Zone1","192.168.0.51:Zone5"]},
"crpc": {"host":"192.168.0.50","pin":"2129918115","debug":true},
"intercom_devices":[{"name":"NAX Speakers","uuid":"a99a07fd-019d-4353-9fb1-af265a49b721",
                     "room_id":52008,"sip_uri":"nax@192.168.0.97"}],
"log_sip": true, "autodetect": true
```

`log_sip` and `crpc.debug` are ON (verbose). Turn them off when done testing.

## Changes made to the PROCESSOR itself (read this)

To make the intercom tile appear with only one panel, a **fake second panel**
("NAX Speakers", device 99001, room 52008 Den) was written into the processor's
config files and the processor was rebooted once (2026-09-07) to load it:

- `/user/Data/Subsystem/Intercom/Intercom.json`
- `/user/Data/PyngDeviceManifest/DeviceManifest.cfg`
- `/user/Data/Subsystem/Locations.json`

Originals and the edited versions are in `Backups/14Malke_MC4R_2026-09-07/`
(`edited/*.after` is what is on the box now). **To undo:**
`python Tools/mc4r_fake_panel.py restore`, then `REBOOT` the processor.

The same fake device is ALSO kept alive at runtime by the bridge's CRPC
registration (`crpc` + `intercom_devices` above), which is what makes it show
Online. Later work at house 6 showed the processor creates the device and group
**dynamically from the registration alone**, so the file edit is probably not
needed. Here it is in place and both agree, so leave it unless restoring.

Also on the HA box: a stale `/addons/ravabridge` folder from an early local
install. Uninstalled, harmless. **Never deploy by writing into `/addons/<slug>`**
on a repo-installed house. It creates a duplicate `local_` add-on that races the
real one for ports.

## What is PROVEN here

- **Doorbell to panel**: a real Side Door press rings the panel with video
  (H.264 pt 103 on multicast `227.1.1.2:40002`), answered, unlock works.
- **Power-failure recovery**: the house lost power on 2026-09-07; everything came back.
- **Page to NAX relay chain** (2026-09-08 19:14): a synthetic page injected at the
  bridge (`python Tools/rava_fake_page.py 192.168.0.97 3`) was decoded and
  streamed; the sender routed Den + Kitchen, played 3.0 s (864000 bytes), restored
  both zones, answered 200. So page audio to speakers works end to end.
- **CRPC registration**: the bridge registers as a real intercom unit; the
  processor shows it Online and Callable.

## What does NOT work, and why (the boundary)

**A page whose only target is our bridge never sends.** The panel hangs on
*Preparing to Page* and never even attempts SIP to us. There is a
processor-internal `IsReadyForCall` flag (separate from Online and Callable) that
a real panel has and ours does not; nothing reachable on this processor shows
what sets it. Setting a numeric `SipUri` (which populates `Intercom.json` fine)
did NOT change this.

Consequence at 14 Malke: with ONE real panel, every page's only other target is
our bridge, so **pages here do not send at all yet.** A page needs at least one
REAL panel among its targets; then it sends, the audio goes out as G.711 u-law
RTP on multicast `227.1.1.1:1234`, and the bridge relays it. Proven at house 6
(2 panels): a mixed group (real panel + our device) sends fine.

## The pending test (a second real panel is being brought here)

1. Commission the new panel into Crestron Home, give it a room, make sure it is
   in the **Whole House** page group.
2. From the Kitchen panel, page Whole House; hold, talk, release.
3. Expect the page on the **Den + Kitchen** NAX speakers about 2 to 3 s behind the
   voice (amplifier routing time; the whole page plays late rather than clipped),
   running about 3 s past release, then the zones restore.
4. Watch it: on the HA box `ha addons logs 60ba3119_ravabridge | grep -E "page relay|page:"`,
   or open `http://192.168.0.97:8098/pages` (shows the `relay` counters).

If it hangs on "Preparing to Page" with the new panel present, the new panel is
not yet a valid target (a commissioning issue), not a bridge problem.

## Where the code is

- `RavaBridge/`: `bridge.py` (SIP and door), `pages.py` (page listener + relay
  hooks), `pageaudio.py` (G.711 to 48k S24BE + chunked stream), `crpc.py`
  (processor registration), `api.py`, `discover.py`.
- `NaxAes67Sender/api.py`: the `/live` endpoint (additive; `/announce` untouched).
- `Tools/rava_fake_page.py` (synthetic page), `Tools/mc4r_fake_panel.py`
  (processor config edit and restore for THIS house), `Tools/cres_console.py`
  (processor console over SSH), `Tools/rava_probe.py` (SIP probe),
  `Tools/crpc_register.py`, `Tools/crpc_samples/REGISTRATION.md` (the protocol).
- Commits: `2027f03` relay build, `c51bda1` fake-page tool, `669a7d9` notes.

## Gotchas learned here

- Panel `APPRESTART` does not drop its CRPC session; only a panel reboot does.
- `SetSipUri` with a non-numeric user (`nax@...`) is accepted but useless; use a
  numeric ext if it ever matters (it does not for the multicast relay).
- The core_ssh add-on is on the internal 172.30.x network and cannot see LAN
  multicast; only host-network add-ons (the bridge) can.
- `Intercom.json` IS the intercom address book (device to SipUri). There is no
  separate phonebook file.
