# Rava Bridge

A door station or phone that speaks ordinary SIP rings the Crestron Home touch
panels, with the door's picture on screen while it rings, **without changing
anything about the panels**. It replaces the AVLinkPro box.

```
UniFi Access intercom --REGISTER / INVITE--> bridge --INVITE, one per panel--> every panel in the group
                      --RTP H.264--------->        --one multicast stream--->  all of them, while ringing
                      <--RTP G.711-------->        <--RTP G.711------------>  the one that answered
UniFi Talk phones     <-- bridge registered into Talk as a third-party device -->   (audio only)
Crestron Home intercom: untouched. The panels never register anywhere.
```

Home Assistant add-on, standard-library Python, one process. `sip.py` and
`rtp.py` are HomeUI's, copied in because an add-on builds from its own folder.

## Why it is shaped like this

**The panels stay in Rava peer-to-peer mode.** That is the constraint the
whole design hangs off. A Crestron Home panel put into SIP *server* mode -
registered to a PBX, which is how AVLinkPro's documented setup works - loses
Crestron Home's own intercom: room paging and room-to-room calls. Crestron's
own CAME/AVLinkPro guide lists "calls from Crestron touch screens to other
touch screens" as not supported in that configuration. So this bridge never
asks a panel to register. It rings a panel the way a 2N or a DoorBird does: an
INVITE straight to the panel's IP.

**What a Crestron Home panel does with such an INVITE** - measured on a
TSW-770R, firmware 3.003, at 14 Malke, September 2026, read-only console plus
three test rings:

- `SIPINFO`: connection mode `PEER`, never registered, local extension a
  ten-digit number Crestron Home assigns (`2053066235`), local name `CRESTRON`,
  page groups = Crestron Home's room groups (`RG_WHOLE_HOUSE_52994,...`),
  paging multicast `227.1.1.1:1234`.
- Codecs G.722, PCMU, PCMA. Video is H.264 on **payload type 103**. DTMF
  payload 96, RFC 2833 support `FALSE` - a panel's digits come as SIP INFO.
- A plain `RTP/AVP` INVITE from an arbitrary IP, even off-subnet over a VPN,
  gets `100 Trying` and then `183 Session Progress` with SDP: audio PCMU
  `sendrecv`, video H.264 `recvonly`. **The panel asks for the picture while
  ringing.** SRTP is shown as "mandatory" in `SIPSETTINGS` and is not enforced.
- A video m-line whose `c=` is a **multicast** address is accepted and echoed
  back (`c=IN IP4 227.1.1.2`, `a=recvonly`): the panel joins the group.

**So the fan-out is the bridge's job, and it is cheap.** The door sends one
H.264 stream to the bridge; the bridge copies each packet to one multicast
group; every panel in the ring group joins it. Each panel only carries its own
audio, unicast, and only the one that answers gets two-way audio. That is
also why this is not Asterisk: Asterisk's `Dial()` forwards early media from
one callee only and does not push the caller's video to ringing callees.

**A door's picture has to flow before anybody answers.** The bridge answers
the door's INVITE with `183` and an SDP straight away, which is what a UniFi
reader needs to start sending video ("if your SIP phone rings but doesn't
display live video, enable early media"), and every panel gets its INVITE at
the same moment, so the group is listening before the first keyframe.

**Two rewrites on the video, and one insertion.** The payload type becomes
103, the sequence numbers become the bridge's own, and if the door carries
its SPS/PPS only in the SDP (`sprop-parameter-sets`) rather than in-band,
they are sent ahead of the first keyframe. A decoder without parameter sets
shows nothing, silently, forever.

**Whoever answers, answers.** The first panel to send `200 OK` wins; the
others get `CANCEL`, exactly as the Crestron Home intercom behaves. A `200`
that races a `CANCEL` is ACKed and then hung up with `BYE`, so nothing is
left off-hook.

**One door call at a time.** A second door ringing while one is live gets
`486 Busy`; the panels would be "in a call" anyway.

**A restart must not lose the door.** The G3 Reader Pro registers for an hour
and has no idea the bridge was gone for ten seconds while its options were
saved - it happened on the first day. So bindings are written to `/data` and
read back on start, and an INVITE from an address that is not registered but
claims a known door is answered `401` with a digest challenge rather than
`403`: the reader re-sends it with the door's password and the call goes
through. The same challenge is what stops any LAN host from ringing the house
by putting a door's name in its From header.

## Setup

1. **Install the add-on** on the house's Home Assistant (local add-on, this
   folder). Put the UniFi Access console IP and a developer API token under
   `access` if you want the doors found for you and `/unlock` to work.
2. **Let it look.** With `autodetect` on (the default) the bridge finds the
   house at startup, every six hours, and whenever the **Discover** button on
   its page is pressed:
   - **Panels**, by sending one SIP OPTIONS to every host on its own subnet.
     Only SIP stacks answer, and a Crestron panel signs its answer with its
     hostname (`TSW-770R-C442683542EB`). Then `SIPINFO` over SSH with the
     panel password reads the extension and the page groups - which are
     Crestron Home's own room groups, so `RG_1ST_FLOOR_53102` becomes a
     `1st floor` group a door can ring. No password? The panel is still
     added; it answers SIP addressed to any user at its IP, so the extension
     is cosmetic.
   - **Doors**, from the Access API: every reader that advertises third-party
     SIP becomes an account named after its alias (`Side Door` → `sidedoor`)
     with the house PIN, and remembers its Access door id for `/unlock`.
   What is found is written into the add-on's options through the Supervisor,
   so it appears on the Options page like anything typed, and is applied to
   the running bridge without a restart. Discovery only adds and refreshes
   IPs; it never removes or renames what a person entered. Rename panels to
   their rooms there if you like.
3. **UniFi Access.** Interface Designer → the intercom / Reader Pro → Doorbell
   Call → **Third-Party SIP** → Configure: Domain = the HA box's IP, port 5060,
   UDP, User ID = the door's `user`, Password = its `password`. Then add a
   **Recipient Number** - any number; `recipients` can map it to a group,
   otherwise the door's `ring` list is used. Requirements from Ubiquiti:
   Access 4.0.21+, UniFi OS 4.1.22+, G3 Reader Pro 1.12.25+ / G3 Intercom
   1.9.25+. One SIP server per Access deployment.
4. **Ring the doorbell.** Every panel in the group rings with the door's
   name and picture. `GET /status` shows the registration, the call, and how
   many video packets went in and out.
5. **Unlock.** A panel in a Crestron Home door call has no keypad; unlock is a
   Crestron Home Quick Action bound to the UniFi Access lock driver, or HA
   calling `POST /unlock` here (set `access.host/token/door_id`). If a panel
   does send DTMF (SIP INFO), it is relayed to the door as-is: a `*` is how a
   UniFi reader unlocks.

**UniFi Talk** (optional, experimental): Talk → Phones → Add Third-Party
Device, copy its SIP host/user/password into `talk`. Talk phones and the app
then ring the panels by dialing that device's extension. Audio only.

## API

Through Home Assistant ingress, or on the LAN with `Authorization: Bearer <api_token>`.

```
GET  /                  a status page with Discover / Test ring / Hang up / Unlock (what ingress opens)
GET  /health            up, address, live calls
GET  /status            panels, doors, registrations, calls, last events, relay counters
POST /discover          find panels and doors now
POST /ring?door=NAME    ring the door's panels with no media - proves the addressing
POST /hangup            end every call
POST /unlock?door=NAME  open that door through UniFi Access
GET  /protect           every Protect camera: type, call, triggers, what it offers, ffmpeg present
POST /protectring?camera=NAME place that camera's call now, as if it had triggered (THIS RINGS THE PANELS)
POST /protectprobe?camera=NAME&seconds=8   pull its media only: proves video and audio flow. Rings nothing
```

## UniFi Protect cameras (the doorbell is a camera, not a SIP device)

A Protect doorbell speaks no SIP, so the bridge stands in for one. Give it the
console and an Integration API key and **every camera is found and listed**,
like Access door discovery. A doorbell is on by default and calls the panels on
**ring**. Any other camera can be switched on too, with its trigger picked from
what Protect offers for that camera: `motion`, a smart detection (`person`,
`vehicle`, `animal`, `package`, `licensePlate`, `face`), a line crossing
(`line`, or `line:person`), loitering (`loiter`), or an audio alarm
(`alrmSmoke`, `alrmBark`, ...). What each camera offers is written into its
`available` list on the Configuration page.

```
press / detection (events WS)  ->  INVITE 127.0.0.1:5060  ->  bridge rings the panels
camera RTSP                    ->  ffmpeg  ->  H.264 (copied) + G.722 (from AAC)  ->  panels
panel voice                    ->  bridge  ->  G.722  ->  ffmpeg  ->  Opus  ->  camera speaker
```

Video is copied through untouched; only audio is transcoded, which is why this
add-on carries **ffmpeg**. The picture starts on the panels while they are still
ringing. Talkback goes out the camera's own speaker when it has one (a doorbell
does; most cameras do not, and `talkback` defaults accordingly).

Set `protect.host` (the console IP) and `protect.api_key` (Protect app ->
Settings -> Control Plane -> Integrations) on the add-on's Configuration tab,
restart, then **Open Web UI**: every camera is listed there with a Call
checkbox, its own trigger list, which panels it rings, talkback and quality.
Save applies at once and writes back to the options - no restart.

The cameras are deliberately NOT edited on the Configuration tab. Home
Assistant renders a list of objects as a YAML blob rather than form fields, so
the controls live on the bridge's own page instead, next to the panels and
doors they act on.
A motion/detection trigger has a 60 s cooldown per camera; ring has 3 s.

**The audio is G.722, not G.711.** A panel lists G.722 first among its codecs
and a Protect doorbell records at 16 kHz, so they meet at the same rate and
nothing is thrown away. G.711 would halve it to 8 kHz and quantise it to eight
bits, which is what made an early build sound like a telephone. The bridge
copies audio packets rather than transcoding them, so the panel is offered
exactly the codec the door is sending and nothing else.

**A doorbell stops reading faces while you are talking to it.** Left alone it
goes on recognising the visitor for the whole conversation, granting or denying
access over and over, because Protect has no idea a call is happening. So `face`
is lifted out of that camera's detections for the length of the call and put
back at the end. What was there is written to `/data` first, so a crash
mid-call restores it at the next start rather than leaving a camera half-armed.
Turn it off per camera with **Pause face** on the page.

**Stream quality matters more than it looks.** Measured on the G6 Entry with
the probe: `high` takes **5.4 s** to its first video packet - a panel ringing
with a blank screen - at 3.2 Mbit/s, and drops packets; `medium` starts in
**2.5 s** at 400 kbit/s and loses none; `low` starts in 1.4 s. A panel is
1280x800, so `high` was only ever downscaled. `medium` is the default.

**Checking a camera without ringing anybody.** `POST /protectprobe?camera=NAME`
pulls that camera through the very same ffmpeg a call uses, into throwaway
sinks, and reports what arrived: packets, payload type, packets per second,
how long before the first one, and any sequence gaps. G.711 should sit at ~50
a second (`audioSteady`). No SIP, no panels. `POST /protectring?camera=NAME`
does place the real call - it rings every panel in the camera's `ring` list,
so keep it for when somebody is expecting it. `protect.debug: true` logs every
event the console sends, which is how to watch triggers arrive without calling.

## Options

| | |
|---|---|
| `autodetect`, `panel_user`, `panel_password`, `subnet` | find panels and doors; the panel console login; a subnet to scan other than the bridge's own |
| `panels[]` | `name`, `host`, optional `ext` (blank = `CRESTRON`), `port`, `groups[]`, `hostname` |
| `doors[]` | `name`, `user`, `password`, optional `host` (accept unregistered calls from it), `door_id` (Access door for `/unlock`), `ring[]` = panel or group names, or `all` |
| `recipients` | dialed number → group/panel list; falls back to the door's `ring` |
| `mcast`, `mcast_port`, `mcast_ttl` | the video group, default `227.1.1.2:40002` ttl 16. The panels' own paging group is `227.1.1.1:1234`; keep clear of it |
| `ring_seconds` | how long the bridge lets a door ring before `480` |
| `realm` | what the door sees in the digest challenge |
| `log_sip` | print every SIP message. The switch to flip when a door will not ring |
| `talk` | UniFi Talk third-party device credentials |
| `access` | UniFi Access console, developer API token and door id, for `/unlock` |
| `protect` | UniFi Protect: `host`, `api_key`, `autodetect`, `ring[]`, `cameras[]` (`name`, `camera_id`, `call`, `triggers[]`, `ring[]`, `talkback`, `quality`; `type`/`available` filled in). Needs ffmpeg (in the image) |
| `api_token`, `api_port` | the LAN lock on the API, default the dealer PIN |

## Testing without a house

`Tools/fake_panel.py` is a pretend panel that answers the way the TSW-770R was
measured to, and `Tools/rava_probe.py` is a pretend door:

```
python Tools/fake_panel.py 5070 3        # answers after 3 s
python Tools/fake_panel.py 5071 0        # never answers, so it gets cancelled
python RavaBridge/main.py RavaBridge/localtest.json
python Tools/rava_probe.py invite 127.0.0.1 0005 --ring 10
```

## Status

- Loopback: door → bridge → two fake panels, answer, cancel of the other,
  BYE both ways: passes (2026-09-07).
- **From the house LAN (2026-09-07, 14 Malke):** the add-on on the HA box at
  .97 rang the real TSW-770R. The panel's 183 put `c=IN IP4 227.1.1.2` on the
  video line - it joined the bridge's multicast group - and audio unicast to
  its own IP. Cancel was clean both ways (200 + 487 + ACK). Nobody answered
  that test, so two-way audio through the bridge is still unproven on a real
  panel (proven on loopback).
- **First real doorbell press (2026-09-07, 14 Malke): worked.** The G3 Reader
  Pro registered (its stack calls itself `mjsip`), dialed, the panel rang with
  the picture, was answered after 9.5 s, and 1,540 video packets went in and
  1,540 out over the multicast group. The reader sends H.264 Baseline 3.0
  (`profile-level-id=42801e;packetization-mode=1`) on payload type 97; the
  bridge rewrites it to 103. What that call also taught: the panel
  **re-INVITEs the instant it answers**, offering G.722/PCMU/PCMA and a new
  audio port, and 0.2.2 refused that as a call from an unknown door - the
  panel hung up two seconds later. 0.2.3 answers the refresh with the same
  media, updated to the new audio port; the fake panel now does the same
  refresh so the loopback test covers it.

## Deploying to a house

The Supervisor treats `/addons/<slug>` as a local add-on. From this repo:

```
tar -C RavaBridge -cf - Dockerfile build.yaml config.yaml run.sh main.py bridge.py api.py sip.py rtp.py README.md   | ssh root@<ha-ip> 'mkdir -p /addons/ravabridge && tar -C /addons/ravabridge -xf -'
ssh root@<ha-ip> 'ha store reload && ha addons install local_ravabridge'
```

Five things that cost time the first day: it is `ha store reload` that picks
up a new folder (the add-on reload does not); an options POST to the
Supervisor must carry **every** option key or it fails with "Missing option";
the schema has no free-form map type, which is why `recipients` is a list; a
plain restart does not pick up changed files, because the image is built at
install - bump the version and `ha addons update`; and `run.sh` must start
with `#!/usr/bin/with-contenv bash`, because the Supervisor hands
`SUPERVISOR_TOKEN` to the container through s6's environment directory and a
script started outside it never sees the variable. Without that, discovery
finds the house but cannot write what it found into the options; it then
keeps its findings in `/data/discovered.json` and folds them in at start, so
the house still rings after a restart either way.
