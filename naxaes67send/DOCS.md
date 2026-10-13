# NAX AES67 Sender

An always-on AES67 stream a DM NAX can be switched to, and an API that speaks
into it. This is the add-on half of HomeUI's paging; the other half is the
page that records a voice and posts it here.

**v0.1.0 proved the path** at 14 Malke on 2–3 Sep 2026 with a 440 Hz tone.
v0.2.0 keeps the same stream and makes it carry real audio on demand.

## Why it is shaped like this

**The stream must never stop.** If the sender stops, the NAX drops the session
from its discovered list and routing a zone to `Aes67` silently will not take.
So the pipeline runs from boot carrying silence, and an announcement changes
what is in it rather than starting it. That is also why the audio comes in
through an `appsrc` instead of swapping a `filesrc`: a pipeline rebuilt per
announcement is a pipeline that is briefly absent, and being briefly absent is
the one thing that breaks the binding.

**Everything stays on the house LAN.** AES67 is PTP-synced multicast and the
NAX's control socket is `wss://<ip>/websockify` — neither leaves the house. So
this add-on does the routing as well as the audio, and HomeUI reaches it
through Home Assistant's ingress rather than trying to do any of it itself.

## The three rules that cost a session

1. 🔴 **Login needs an `Origin` header.** Without one every source IP gets a
   403 that looks exactly like a locked account. Rebooting the amplifier —
   the obvious response — achieves nothing.
2. 🔴 **NaxRx writes are accepted and ignored over REST.** They return 200 and
   read back wrong. They only apply over the WebSocket.
3. 🔴 **The route clears itself on the first write.** Write it again ~1.5 s
   later and it sticks. Reading that clear as a rejection is the trap.

`naxctl.py` carries all three.

## API

Through ingress, so anything calling it already holds a token for this house.
`api_token` is a second lock for the case where the port is exposed on the LAN.

```
GET  /health          is the stream up, is anything playing
GET  /zones           every zone, its source, whether audio is present
POST /announce?zones=Zone4[,Zone5]   audio in the body -> speak, then restore
     &clip=ID                        ... or a stored clip
     &before=ID|none  &after=ID|none  a sound around it (else the clip's / the house's setting)
POST /scan            find the amplifiers on this network and add the new ones
```

`/announce` is synchronous — the caller wants to know whether a page was
actually made. It takes the length of the recording plus about four seconds of
routing. One announcement at a time; a second gets `409` rather than being
queued, because by the time the first finished a queued one would be stale.

Any format GStreamer can decode is accepted; the browser sends WebM/Opus. It
is decoded to the S24BE/48k/2ch the NAX expects, in a separate process so a
strange upload cannot wedge the live pipeline.

## The announcements every house starts with

`announcements/` in the add-on ships 36 spoken clips, seeded into
`/share/nax-announcements` the first time the add-on starts on a house - so the
tile is never empty on a new install. 19 are on the tile
(Breakfast Ready, Check Your Phones, Dinner Is Served, Family Meeting, Family Movie Night, Food Delivery Arrived, Good Morning, Head To Practice, Ice Cream Ready, Kids Come Downstairs, Laundry Finished, Lunchtime, Quick House Sweep, Quiet Hours, School Bus Five Minutes, Shoes And Backpacks, Someone To Front Door, Trash And Recycling, Weekend Mode); 17 are hidden, for Actions & Events only
(Back Door Opened, Back Door Visitor, Front Door Opened, Front Door Visitor, Front Gate Visitor, House Armed, House Arming, House Disarmed, Man Gate Visitor, School Bus Outside, Shabbat Over, Shabbat Starting, Side Door Opened, Side Door Visitor, Side Gate Visitor, Vehicle Entering, Vehicle Exiting).

Seeding is once per clip and remembered in `/data/seeded-announcements.json`:
a bundled clip a person deletes stays deleted through restarts and updates, a
house's own clip with the same id is never overwritten, and a clip added to a
later release is seeded when that release arrives. The add-on's own page
(Open Web UI) lists them, plays them back, uploads any audio file, speaks a
typed sentence into a new clip, and hides or deletes any of them.

The page needs no token through Home Assistant's ingress - it already made the
person log in. The `api_token` still locks the port on the LAN.

## A sound before or after the words

"Ding-dong, someone is at the front door." Sounds are clips too - the shipped
set includes `ding-dong` (a two-tone doorbell) and `chime` (one soft tone),
hidden from the tile and flagged `sound` - and they are joined to the words
**at play time**, with a 0.3 s breath between, so the recording stays clean
and the sound can be changed later without re-recording anything.

Who decides, most specific first:

1. the request: `before=chime`, `after=none` on `/announce`;
2. the clip's own setting (the page's *before* / *after* columns; `POST
   /clipchime?id=&before=&after=`, each a clip id, `none`, or `default`);
3. the house default: `chime_before` / `chime_after` in the options, also on
   the page.

The eight door, gate and delivery lines ship set to ring the doorbell first.
A recorded-on-the-spot announcement has no clip, so it takes the request or
the house default.

Eight sounds ship: `ding-dong`, `chime`, and since 0.13.0 `westminster`
(the Westminster Quarters, E C D G / G D E C), `three-chime`, `tubular-bells`,
`marimba`, `old-bell` (a mechanical ringer) and `electronic` (a two-tone
bing-bong). They are synthesised in plain Python by `tools/make_sounds.py` -
re-run it to change one, then commit the WAVs. The page's **Doorbell & chime
sounds** card plays each one in the browser, and the house-default selects
have a Play beside them; nothing is sent to the speakers.

## Editing what an announcement says

Every clip that is words (not a sound) has an **Edit** button: the text in a
box, a speed slider (0.5x-2x), a voice, **Play** to hear it in the browser, and
**Regenerate** to replace the clip's audio. The clip keeps its id, name, tile
setting and sounds, so Crestron programming that plays it is unaffected.

* **Speech is Fish Audio** when a key is found - the engine the shipped set
  was made with. The key lives in the house's private
  **`/config/.bav_fleet.json`** as `fish_api_key` - the file the image seeder
  writes and `bav_house` reads its Cloudflare token from - so every house can
  share one key without it being in this public repository. **Nobody types it:**
  at every start (every install and update) the add-on asks our Hub,
  `https://logs.bav.homes/house/fish-key`, with HTTP Basic `admin` + the device
  PIN (`amp_password`, else `crpc_pin`), checks the answer with Fish, and keeps
  it in that file (mode 600). A Hub that is down at boot costs nothing - the
  house keeps the key it has - and a key changed on the Hub reaches every house
  on its next restart. `key_server` in the options points elsewhere, or `off`
  stops the fetch. The page's key box writes the same file by hand. A
  `fish_api_key` typed into this add-on's own options overrides the file, for a
  house on a different account. It is a *developer*
  key, billed from the API-credit wallet, which is separate from any
  subscription: a `402` means that wallet is empty. Without a key, typed text
  falls back to Home Assistant's TTS, then espeak-ng, with no choice of voice.
* **Voices:** Jarvis (the shipped set's voice, and the default - `fish_voice`
  changes the house default) plus eight more, chosen from fish.audio's public
  English voices by use: the seven "Fish Official" voices in the top 400
  (Sarah, Adrian, Selene, Ethan, Hannah, Jordan, Laura) and the most-used calm
  British narrator. The raw top of that list is game announcers, meme voices
  and clones of real people, which were passed over.
* **What Play heard is what Regenerate saves.** Fish never reads a sentence the
  same way twice, so the last few takes are kept in memory and Regenerate
  reuses the matching one instead of asking again.
* **The shipped clips never had their words saved.** The first time one is
  opened in the editor it is transcribed (Fish speech-to-text, same key) and
  the text kept in its `.json`, so it is paid for once. Without a key the box
  starts with the clip's name.

```
GET  /voices                                the choices, the default, whether Fish is set up
GET  /cliptext?id=                          what a clip says
POST /tts?text=&voice=&speed=               speak without saving (the editor's Play)
POST /clipregen?id=&text=&voice=&speed=     speak new words into an existing clip
POST /fleetkey  {fish_api_key}              check a key with Fish, keep it in the fleet file
```

## The zone list is the house's, not the rack's

`GET /zones` - what the driver's Announcements page, HomeUI and the settings
page all show - is shaped in two ways since 0.10.0:

* **A bussed pair is one room.** A DM-NAX can bus two outputs (a stereo zone
  and a bridged-mono partner, `IsBussed`/`BusId` on both); listed raw, "The
  Snug" appeared twice. Measured at 110 Roosevelt: routing the primary alone
  switches, raises and restores the partner with it. So the pair is one entry,
  keyed by its lowest-numbered zone, with `members`; paging either member pages
  the primary once.
* **Crestron Home's media rooms set the list.** With `crpc_host` set, the
  processor's room list (43 there, against 36 amplifier zones) is the list, in
  its order; each room's speakers are matched by name (case and apostrophes
  ignored). A room with no matching zone is still shown - as *"Room (no
  speakers found)"*, `unavailable` set, and refused if it is the only target
  of a page - because a room that silently disappears from the list is the one
  nobody notices. A zone Home has no room for goes last, marked *"(not a room
  in Crestron Home)"*. Without a processor the amplifier order stands.

## Finding the amplifiers

Nobody should have to type six addresses. With `autodetect` on (the default)
the add-on looks for amplifiers at start, every six hours, and when the
**Find amplifiers** button on its page is pressed:

1. every host on the /24 the stream leaves by (or `subnet`) with 443 open;
2. of those, the ones whose web server answers `Server: Crestron Webserver` —
   nothing that is not Crestron does, so this needs no credentials;
3. of those, not the ones whose reverse-DNS name says panel, processor,
   gateway or PDU (`TSW-`, `CP4`, `CEN-`, `PC-350` …);
4. what is left is logged into with `amp_password` — first as `amp_user`,
   then as the other factory username (`admin` / `chdevice` — Crestron ships
   both, by firmware) — and anything that answers `ZoneOutputs` is an
   amplifier. Whichever user worked is what gets saved.

Verified amplifiers are added to `amps` through the Supervisor, so they show
on the Configuration tab like ones typed by hand, and the running add-on can
use them at once. **Adds only** — never removes or rewrites an amplifier a
person entered. A device with `NAX` in its name that refuses both logins is
reported by name and address on the page and in the log rather than dropped,
because that is a wrong password, not a missing amplifier.

Measured at 110 Roosevelt (six DM-NAX, three of them renamed by the
installer): 82 hosts on 443, 26 Crestron, the six amplifiers found, and both
of the ones checked by hand said `403` to `admin` and let `chdevice` in.

## Options

| | |
|---|---|
| `amps` | the amplifiers: `host`, `user`, `password` each. Filled in by auto-detect; add by hand if you prefer |
| `autodetect` / `amp_user` / `amp_password` / `subnet` | see above. `subnet` blank = the /24 the stream leaves by |
| `nax_host` | (older option) one amplifier, e.g. `192.168.0.51` |
| `nax_user` / `nax_password` | `admin` or `chdevice`, Crestron Home common device password |
| `mcast` / `port` | must be inside `239.8.0.0`–`239.128.255.255` |
| `session` | the SAP session name the NAX will show |
| `api_token` | optional bearer for the control API |

## Restoring

A zone is put back in a `finally`. A room left on a silent AES67 input is a
room whose music never comes back, and that is a worse failure than the
announcement not playing at all.
