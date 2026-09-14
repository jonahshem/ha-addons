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
