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
GET  /zones           every zone, its source, whether audio is present,
                      and the speaker groups (Whole House first)
POST /announce?zones=Zone4[,Zone5]   audio in the body -> speak, then restore
     &clip=ID                        ... or a stored clip
     &before=ID|none  &after=ID|none  a sound around it (else the clip's / the house's setting)
POST /scan            find the amplifiers on this network and add the new ones
POST /live?zones=auto streamed PCM in the body -> a page, played as it arrives
POST /repair          put back what the page journal says is still ours, then
                      clear any zone left on the announcement input
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

If the amplifier's websocket is closed, or a write raises, the session is
thrown away, a fresh login and socket are made and the restore is tried again
- four times, with a growing pause - before anything is logged as `COULD NOT
RESTORE`. What a page changed is also written to `/data/page-journal.json`
as it happens and crossed off as it is put back, so a restore that a crash or
a silent amplifier interrupted is finished at the next start, or by `POST
/repair`. That only undoes what is provably still ours: a route goes back
only if the zone is still on `Aes67` (and never *to* `Aes67`), a volume only
if the zone is still at the level the page wrote. A room somebody has touched
since is theirs.

## 0.15.2 — the speaker groups in `/zones`, for the driver's page

The NaxAnnounce driver's page was a box per room - 43 of them at 110
Roosevelt - where Crestron's own Page screen offers *Whole House*, *1st
Floor* and so on. The speaker groups on the settings page are that list
already, so `/zones` now carries them as `groups`:

```
"groups": [{"name": "Whole House", "page_group": "Whole House",
            "zones": ["192.168.1.51:Zone1", ...], "available": true}, ...]
```

In the configured order, except that the group tied to Whole House leads -
by its page group's `WholeHouse` type when the processor answers (the string
`WholeHouse` or the enum's `0`, the form the processor's own Intercom.json
keeps), else by either name - because the driver's default is the first entry.
The driver does no leading of its own, so this is the one place it is decided. A
group none of whose zones is in the live list with speakers is still listed,
`available: false`, so a dead amplifier shows rather than hides a choice.
Zones count as live as the settings page saved them: a bus partner (`Zone6`
folded into `Zone5`) and a bare `Zone4` in a one-amplifier house both do.
Paging a group is `zones=group:NAME`, which `resolve_zones` has taken since
speaker groups existed, with two changes now that a driver sends it from a list
and from saved rules:

- **An unknown group is refused**, in words, instead of paging every zone. A
  typo or a group renamed after a rule was saved used to page the whole house;
  it is now a failed page the driver shows as a Problem. `zones=auto` keeps its
  every-zone fallback - it names no group.
- **Names compare the way room names do** (`zonelist.norm`: case, curly quotes,
  runs of spaces), and a group's name is trimmed when it is read and saved, so
  `1st Floor ` saved with a stray space is the `1st Floor` the driver lists.

NaxAnnounce 1.6.0 is the driver that reads this; an older driver ignores the
key. `tests/test_speaker_groups.py`.

## 0.15.0 — every amplifier at once, and a restore that is never skipped

**What happened.** 69 Phillips, 2026-10-13, the house's first page: seven
DM-NAX (`192.168.1.18`, `.160`–`.165`), 46 zones, a 3.2 s *Page All* relayed by
RavaBridge to `POST /live?zones=auto`. The sender did one amplifier at a time,
and on the first one pointed one never-used receive slot at a time - a REST
read, a write and a 1.5 s settle per zone, 2.5 s each:

```
15:44:07 [nax] 192.168.1.18: [Zone1..Zone8] -> [Zone1,2,3,4,6,7,8] (bussed)
15:44:09 Zone1: receive slot pointed at 'HA Announce 1'; volume 300 -> 700
15:44:11 Zone2: receive slot pointed ...; volume 500 -> 700
   ... Zone3, Zone4, Zone6 ...
15:44:20 [nax] 192.168.1.18: COULD NOT RESTORE (socket is already closed.)
15:44:20 COULD NOT RESTORE VOLUME for Zone1,2,3,4,6
15:44:20 [api] live page failed: WebSocketConnectionClosedException: socket is already closed.
```

No audio anywhere, five rooms left at 700, the other six amplifiers never
reached. Four changes:

1. **Amplifiers in parallel.** `naxctl.announce_many` prepares and routes each
   amplifier in its own thread (zones within one still go one at a time - the
   device's rule). The page starts when every amplifier has landed, or `cap`
   seconds after the first did: 3 s for `/live`, 10 s for `/announce`. An
   amplifier that lands later joins late - its rooms hear the rest of the page
   - and is restored like the others. A live page still trails the panels by
   the routing time; nothing is dropped. The restore is per amplifier and
   parallel too.
2. **Receive slots warmed in the background.** `naxctl.prewarm` points every
   zone's slot at our stream at start-up, after every discovery pass (and so
   every six hours, and whenever discovery adds an amplifier), amplifiers in
   parallel, one log line each. It never routes, never writes a volume, never
   turns a zone on, leaves a slot carrying another session alone, and waits
   while a page is in progress. A page's slot check is then free; when it is
   not, the slots are read once per amplifier rather than once per zone.
3. **The restore is never skipped** (above), and the socket is checked before
   anything audible: one that died while slots were being pointed is replaced
   before the first volume or route write, not discovered by it.
4. **Why the socket closed.** Not known - it needs the amplifier.
   `socket is already closed.` is websocket-client saying it had already read
   EOF: the amplifier, or the network between, closed the TCP connection
   about twelve seconds into a run of receive-slot writes (one every 2.5 s,
   each followed by a 1.5 s settle - not a burst). An earlier draft of this
   entry blamed the pool's `drop` closing a page's session; that could not
   have happened in 0.14.2, where `drop` only ran after `get` had already
   failed. What 0.15.0 changes is how the add-on lives with it: the
   websocket listener (which was `except: pass` around `recv()` and spun flat
   out on a dead socket, telling nobody) now stops, `alive()` goes false
   within 0.6 s, and the log says when the socket closed, how long after it
   opened, and the amplifier's close code if it sent one. The page
   reconnects before it writes, and the slot writes are moved out of the page
   altogether by the warm-up. If the next page logs `websocket closed N s
   after it opened (the amplifier sent close …)`, that line names the cause;
   if it keeps happening, a websocket keepalive or slower slot writes are the
   next things to try - neither is guessed at here.

Also: `ensure_rx` refuses a zone when the slots cannot be read, rather than
reading the failure as "every slot is empty" (which would have taken over a
slot belonging to somebody else); a fresh session's routes are seeded from a
REST read so a clear is never confirmed by silence (this also fixes `/repair`'s
`unstick`); and log lines from parallel threads no longer run together.

**Nothing is crossed off on a read that failed or a write nobody
acknowledged** (second review, before release). `Nax.zones()` turns a failed
REST read into an empty answer, which is right for the tile and wrong for
anything that acts on it; it now takes `strict`, and every caller that decides
something reads strictly: the page's first read (an empty listing would have
recorded `""` as what every room played and cleared its music afterwards),
`recover` (it read every stuck zone as "moved on" and wiped the journal with
nothing put back), and the route seeding after a reconnect (a failed read
seeded nothing, and a clear was again confirmed by silence - a session that
cannot be seeded is not used). `rx_streams(strict=True)` also refuses a reply
without `NaxRxStreams`. A volume restore is read back from the amplifier
before it counts - a send into a half-closed socket succeeds locally - and
`recover` does the same; a room turned by somebody since is left. A zone whose
volume cannot be read is never raised (nothing could put it back, and it may
already be louder). Playback starts when every amplifier's routes have landed,
not when each thread has finished its settle watch and volume read-back,
which saves 1.5-2 s; `/live` now waits out the audio that piled up during
routing before it returns, instead of a fixed 3 s tail that let the restore
cut off the end of the page. Each amplifier's restore waits for its own
preparation only, so one that does not answer no longer holds every other
room on the silent input. A journal entry the house has since changed (another
source, another level) is replaced by what the room has now. A warm-up asked
for while one runs is run again afterwards rather than dropped - it may be for
an amplifier discovery has just added. And `Nax.close()` no longer sleeps
0.8 s when the listener has already stopped.

