# 14 Malke — announcements and paging into the DM NAX speakers: state and how it works (2026-09-09)

Handoff for another session. Everything here was measured live at the house; where something is an
assumption it says so. Test house: credentials are deliberately plain (default PIN `2129918115`).

## The house

| What | Where | Notes |
|---|---|---|
| Crestron Home processor | MC4-R `192.168.0.50` | SSH/SFTP `admin` / `2129918115`. CRPC (the app's own RPC) on TCP 50001. Nightly reboot ~04:01. |
| Panels | Kitchen TSW-770R `192.168.0.225` (device 52988); Social Room panel `192.168.0.17` (device 99032) | Both real, both Online. The fake "NAX Speakers" panel from 2026-09-07 is GONE (see below). |
| NAX amplifier | DM-NAX-8ZSA `192.168.0.51`, fw 3.1.0103 | Zones 1-8: Den, Dining Room, Living Room, Deck, Kitchen, Social Room, Master Bed, Zone8 (unused). Login `chdevice` / `2129918115` (or `admin`). Console SSH `admin`. **Media Player mode MP1 — leave it.** |
| Home Assistant | HAOS Pi `192.168.0.97` (`14malke.bav.homes` via Cloudflare) | Our add-ons run here from the public repo. SSH `root@192.168.0.97` (key in the vault / `~/.ssh/ravabridge_ha`). The Terminal & SSH add-on has `ha` but no docker and its own netns. |
| Switch | UniFi at `192.168.0.1` | IGMP snooping does NOT re-forward a multicast group after the amp leaves it (see gotchas). |

VPN into the house is required (Tailscale on the desktop wedges; restart the service elevated).

## Add-ons (all from github.com/jonahshem/ha-addons, publish with `python Tools/publish_ha_addons.py`)

| Add-on | Version | Role |
|---|---|---|
| NAX AES67 Sender `60ba3119_naxaes67send` | **0.7.1** | Always-on AES67 stream into the NAX on `239.69.4.5:5004`; `/announce` (stored clips), `/live` (streamed page), `/config` + `/ui` (speaker groups, per-zone volume), a CRPC session to the processor for page-group detection and music resume. API `:8099`, token `2129918115`, ingress. |
| Rava Bridge `60ba3119_ravabridge` | **0.5.1** | Listens for panel pages on multicast `227.1.1.1:1234` (G.711), decodes/resamples, streams them to the sender's `/live` with `zones=auto`. API `:8098`. No longer registers with the processor. |
| NAX AES67 Probe | 0.1.0, stopped | Read-only: captures our RTP on the Pi's `end0` for 6 s. Start it, read its log, stop it. |

Deploy = bump `version` in the monorepo add-on's `config.yaml`, commit, `python Tools/publish_ha_addons.py`, then on the Pi
`ha store reload && ha addons update 60ba3119_<slug>`. **Never write files into `/addons/` on the Pi** (creates a `local_` duplicate
that races the real one for the port). If updates are refused with `'AppManager.update' blocked ... supervisor needs to be updated
first`, run `ha supervisor update` (~90 s, add-ons keep running).

## What is live and how it works

### Stored announcements (Crestron driver `NaxAnnounce` 1.4.0 + sender `/announce`)
36 clips in `/share/nax-announcements` (Fish Audio, the Jarvis voice). `POST /announce?clip=<id>&zones=<list|auto|group:NAME>`.
For each zone: remember its source, raise its volume to the configured floor if below, bind it to the `Aes67` input, play,
restore source and volume. Timing on a warm session: audio ~2.0 s after the request for one zone, ~4.6 s for five.

### Music comes back after a page (0.6.x)
The amp never pauses anything — **Crestron Home does**: it sees the zone leave its player and pauses it (processor log:
`Externally cleared route ... Media Player state changed to Paused`). The sender therefore holds a CRPC session to the
processor (`crpcmedia.py`, its own client uuid in `/data/crpc_uuid`, registered at startup, kept warm), polls what is
playing (`IRpcMedia.GetSubsystemState {stateRevstamp}` every 2 s — null when unchanged), and right after the route restore
sends the same call the app sends: `IRpcMediaSources.SendCommand {sourceId, command:"Play", triggerType:"Pulse",
targetId:0, useAliasing:false}`. Zone ↔ media room is matched by NAME. Only rooms that were Playing are resumed.
Proven repeatedly on the Deck with Pandora (music back ~11 s after the request).

### Live pages from the panels (RavaBridge → sender `/live`)
Panel page → G.711 on `227.1.1.1:1234` → bridge → chunked POST to `/live?zones=auto` while the page is still being spoken →
sender routes the zones, raises volumes, plays as it arrives, restores, resumes music. Audio trails the voice by the
routing time (~2 s one zone, ~4.5 s five) and runs ~3 s past release. Five clean pages in a row tonight after the fake panel
was removed. With one real panel a page never sends (processor-internal `IsReadyForCall`); two real panels fixed that.

### Speaker groups and per-zone volume (0.7.x) — the settings page
Open the sender add-on → **Open Web UI** (`/ui`). It reads the amp's zones and the processor's page groups live
(`IRpcIntercom.GetPageableRoomGroups {intercomRevStamp:0}`; here: **Whole House** 52994, **1st Floor** 53102, **Cellar** 99041).
You build speaker groups from zones, tie each to a page group, and set an announce-at percent per zone (default 70). Saves go
`POST /config` → Supervisor `addons/self/options` AND `/data/settings.json` (the Supervisor rewrites `/data/options.json` only on
start — that is why 0.7.1 exists). `zones=auto` resolves the page group being pressed right now (the sender subscribes to
`IRpcIntercom.RequestPageableRoomGroupsChangedEvents` and keeps `active_page` from `IRpcIntercom.Event`) → its speaker group, or
the group with the same name; `zones=group:NAME` by name; no match → every zone. Seeded tonight: Whole House → Zones 1-6,
1st Floor → 1-5, Cellar → Zone6; all zones at 85% for the test.

**Not yet proven on a real page:** the live page-group → speaker-group resolution (coded from a captured event; nobody paged
during the capture window). The sender logs `[crpc] page CallStarting: groups [...]` and `[api] zones: page group 'X' -> speaker
group 'Y'` when it happens. Ask for one page per group and read those two lines.

## Gotchas that cost real time (do not rediscover)

- **Never write `StopRequested` to a NAX receive slot.** It makes the amp leave the multicast group and the UniFi switch never
  re-forwards it; every slot sits on `Connecting`. Only a NEW group cured it — that is why the stream is on `239.69.4.5` (the
  option, not the config default). Durable fix is an IGMP querier on that VLAN (Jonah's call). Slots are corrected by overwriting.
- **MP2 mode is incompatible with Crestron Home on this firmware**, even after a CH repair (tried twice). In MP1 the
  `MediaPlayerNeXt` object is inert and a `RequestAction` there stalls the amp's web API ~2 min. Never send those in MP1.
- The amp's XSRF token is a **login response header**, not a cookie (only matters for REST POSTs; GET/WS don't need it).
- CRPC facts: TLS :50001, `0x26` connect frame with `clientdevice:<PIN>`, `Crpc.Register`, JSON-RPC in `0x14` frames, 3 s heartbeat;
  replies span frames (accumulate and brace-match); a brand-new client uuid registers fine; every read takes a `...Revstamp`
  parameter and answers null when unchanged (`systemRevstamp`, `stateRevstamp`, `intercomRevStamp`, `roomListRevstamp`,
  `deviceHealthRevstamp`). Framing lives in `RavaBridge/crpc.py` and `NaxAes67Sender/crpcmedia.py`.
- The processor's Seawolf log (`/rm/SeawolfDiagnostic/<date>.log`, SFTP) shows media commands at its default level — read it
  before planning any packet capture (the CH↔NAX link is TLS anyway).
- Zone volumes are the amp's 0-900/1000 scale; Crestron Home's percent × 10. dB per step differs per zone.
- An edit helper that opens a file for writing before computing the content will empty it when the edit fails (it did, once).

## Processor config edits (history)
2026-09-07 a fake "NAX Speakers" panel was added (device 99001 etc.) to make pages send with one panel. **Removed 2026-09-09**
by editing the LIVE files in place (device 99001 from `DeviceManifest.cfg`, pageable room 99003/device 99002 from `Intercom.json`,
ids 99001+99004 from the Den in `Locations.json`) and `REBOOT` — NOT via `Tools/mc4r_fake_panel.py restore`, whose Sep-7 originals
predate the second panel. Backups: `/user/Backups/intercom-2026-09-09-before-removal` on the MC4-R and
`Backups/14Malke_MC4R_2026-09-09-before-removal/` in the monorepo.

## Open items
- Prove `zones=auto` on a real page (above). Then decide the default groups and the real per-zone levels (70 is the spec default).
- First page after a sender restart pays ~2 s more (the amplifier session is cold); warm it at startup like the CRPC one.
- The 7 NAX streaming presets were lost in the MP2 experiment (list in memory / `nax_mp1_snapshot_*.json`); re-save in the app.
- Master Bed panel 52340 is Offline and "Touch Screen Remote" 52769 is Unknown on the processor — not page targets today.
- Crestron support question (optional): does Crestron Home OS support a DM-NAX-8ZSA in Media Player 2 mode?

## Key files
`NaxAes67Sender/` — `api.py` (HTTP surface: announce, live, config, ui), `naxctl.py` (amp: routes, volumes, receive slots, pool),
`crpcmedia.py` (processor: resume, page groups, active page), `settings.html`, `sap.py`, `sender.py`, `clips.py`.
`RavaBridge/` — `pages.py`, `pageaudio.py` (relay), `crpc.py`, `bridge.py`. `NaxAnnounce/` — the Crestron Home driver.
Commits: 0.5.2 refusals/route guard, 0.5.3, 0.6.1-0.6.4 resume, 0.6.5 pipelined routes, 0.7.0/0.7.1 groups+settings.
