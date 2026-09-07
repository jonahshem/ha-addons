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
```

`/announce` is synchronous — the caller wants to know whether a page was
actually made. It takes the length of the recording plus about four seconds of
routing. One announcement at a time; a second gets `409` rather than being
queued, because by the time the first finished a queued one would be stale.

Any format GStreamer can decode is accepted; the browser sends WebM/Opus. It
is decoded to the S24BE/48k/2ch the NAX expects, in a separate process so a
strange upload cannot wedge the live pipeline.

## Options

| | |
|---|---|
| `nax_host` | the amplifier, e.g. `192.168.0.51` |
| `nax_user` / `nax_password` | `admin` or `chdevice`, Crestron Home common device password |
| `mcast` / `port` | must be inside `239.8.0.0`–`239.128.255.255` |
| `session` | the SAP session name the NAX will show |
| `api_token` | optional bearer for the control API |

## Restoring

A zone is put back in a `finally`. A room left on a silent AES67 input is a
room whose music never comes back, and that is a worse failure than the
announcement not playing at all.
