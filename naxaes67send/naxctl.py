"""Switching a DM NAX zone to the AES67 stream, and back.

Every rule in here was measured against the DM-NAX-8ZSA at 14 Malke on
2-3 Sep 2026. None of it is inferred from documentation, and two of the rules
look like bugs on the way in:

  * 🔴 **Login needs an `Origin` header.** Without one, every source IP gets a
    403 that looks exactly like an account lockout. It is not one, and
    rebooting the amplifier to clear it - which is the obvious thing to do -
    achieves nothing.

  * 🔴 **NaxRx writes are accepted and ignored over REST.** They return 200,
    they read back wrong, and they only take over the WebSocket. The device's
    own configuration app drives everything over `wss://<ip>/websockify`.

  * 🔴 **The route clears itself on the first write.** Setting
    `Routes/ZoneN/AudioSource` makes the device transiently blank it, which
    happens to its own web UI too. Write it again about 1.5 s later and it
    sticks - two writes in practice. Reading that clear as a rejection is the
    trap that cost most of the session this came from.

Standard library plus `websocket-client`, which the add-on image installs.
"""
import json
import os
import ssl
import threading
import time
import uuid
import urllib.error
import urllib.parse
import urllib.request
import http.cookiejar

import websocket

SOURCE = "Aes67"       # what the amplifier calls our announcement input
SETTLE = 1.5           # how long the device takes to decide a route stuck
ATTEMPTS = 6           # two is normal; six is for a device having a bad day
RESTORE_TRIES = 4      # a restore on a dead socket: reconnect and try again this often
RESTORE_BACKOFF = (0.5, 1.0, 2.0)   # seconds between those tries
PREP_JOIN = 60.0       # how long the restore waits for an amplifier still preparing


def _zone_order(name):
    """Zone10 after Zone9, not between Zone1 and Zone2."""
    tail = str(name).replace("Zone", "").strip()
    return (0, int(tail)) if tail.isdigit() else (1, str(name))


def mark_buses(zones):
    """Fold bussed zones into one: the lowest-numbered member of a bus is the
    primary and carries `members`; the others get `bus_of` naming it. A bus
    with one member here (its partner elsewhere, or none) is left alone. In
    place, and returned."""
    groups = {}
    for name, info in zones.items():
        if info.get("bussed") and info.get("bus") not in (None, 0, "0"):
            groups.setdefault(info["bus"], []).append(name)
    for members in groups.values():
        if len(members) < 2:
            continue
        members.sort(key=_zone_order)
        primary = members[0]
        zones[primary]["members"] = list(members)
        for other in members[1:]:
            zones[other]["bus_of"] = primary
    return zones


class NaxError(RuntimeError):
    pass


class SlotTaken(NaxError):
    """A receive slot carrying somebody else's session - see `ensure_rx`."""


class Nax:
    """One amplifier: log in, hold a WebSocket, route zones."""

    def __init__(self, host, user="admin", password="", log=print):
        self.host = host
        self.user = user
        self.password = password
        self.log = log
        self.jar = http.cookiejar.CookieJar()
        self._opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(self.jar),
            urllib.request.HTTPSHandler(context=self._lax()))
        self.xsrf = ""
        self.ws = None
        self.routes = {}
        self.landed = {}          # zone -> when its route was seen to bind
        self._stop = False
        self._thread = None
        self._rx_ok = set()       # receive slots already confirmed as ours
        self.opened_at = None     # when the websocket was opened
        self.closed_why = None    # set by the listener when the socket dies under us
        # websocket-client's send is not thread-safe, and a pooled connection
        # is shared: an announcement writing routes while the tile reads zones
        # would interleave two frames into one.
        self._wlock = threading.Lock()

    @staticmethod
    def _lax():
        # A Crestron device signs its web UI with its own certificate. There
        # is no CA to check it against and pinning it would break on every
        # firmware update, so this trusts the address instead - which is the
        # same trust the installer's browser extends when it clicks through.
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        return ctx

    def _url(self, path):
        return f"https://{self.host}{path}"

    def _headers(self):
        return {
            # Both of these, on every request. See the Origin note above.
            "Origin": f"https://{self.host}",
            "Referer": self._url("/userlogin.html"),
            "User-Agent": "HomeUI-NaxAnnounce/1.0",
        }

    # -- getting in -------------------------------------------------------
    def login(self):
        body = urllib.parse.urlencode(
            {"login": self.user, "passwd": self.password}).encode()
        req = urllib.request.Request(self._url("/userlogin.html"), data=body,
                                     headers=self._headers())
        try:
            with self._opener.open(req, timeout=20) as r:
                r.read()
        except urllib.error.HTTPError as e:
            if e.code == 403:
                raise NaxError(
                    "403 from the amplifier. If the password is right this is "
                    "almost always a missing Origin header rather than a "
                    "locked account - do not reboot it.") from e
            raise NaxError(f"login failed: {e.code}") from e
        except urllib.error.URLError as e:
            raise NaxError(f"cannot reach {self.host}: {e.reason}") from e

        for c in self.jar:
            if c.name == "CREST-XSRF-TOKEN":
                self.xsrf = c.value
        if not any(c.name.startswith("iv") or "CREST" in c.name for c in self.jar):
            raise NaxError("logged in but the device set no session cookie")
        return True

    def _cookie_header(self):
        return "; ".join(f"{c.name}={c.value}" for c in self.jar)

    # -- REST, for reading -------------------------------------------------
    def get(self, path):
        req = urllib.request.Request(self._url(f"/Device/{path}/"),
                                     headers=self._headers())
        with self._opener.open(req, timeout=15) as r:
            # utf-8-sig: the Media Player 2.0 objects come with a byte-order
            # mark that plain utf-8 turns into a JSON error.
            return json.loads(r.read().decode("utf-8-sig", "replace")).get("Device", {})

    @staticmethod
    def _label(obj):
        """Whatever this amplifier calls a zone, if it calls it anything.

        Written by looking rather than by knowing: the firmware here was never
        dumped field by field, so this takes the obvious keys first and then
        anything ending in `Name` that holds a non-empty string. A zone the
        device has not named comes back empty and gets numbered instead - the
        house renames them in Settings either way, and guessing a name would
        be worse than admitting there isn't one.
        """
        obj = obj or {}
        for key in ("Name", "ZoneName", "UserName", "Label", "FriendlyName"):
            v = obj.get(key)
            if isinstance(v, str) and v.strip():
                return v.strip()
        for key, v in obj.items():
            if key.endswith("Name") and isinstance(v, str) and v.strip():
                return v.strip()
        return ""

    def zones(self, strict=False):
        """Every zone: what it is called, its source, whether audio is there.

        `strict` raises when either read failed. Without it a failed read is
        an empty one - fine for the tile's list, and wrong for anything that
        acts on the answer: an empty listing says every zone is on nothing at
        no known volume, so a page would record "" as what each room was
        playing and clear the music afterwards, and `recover` would read every
        stuck zone as "moved on" and forget it. Those callers read strictly.
        """
        # Two independent reads, so take them at the same time rather than one
        # after the other - this is the last REST round trip standing between a
        # page being asked for and the route being written.
        got, failed = {}, {}

        def fetch(key, path):
            try:
                got[key] = self.get(path)[path][key]
            except Exception as e:
                got[key] = {}
                failed[path] = f"{type(e).__name__}: {e}"

        threads = [threading.Thread(target=fetch, args=a) for a in
                   (("Routes", "AvMatrixRouting"), ("Zones", "ZoneOutputs"))]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        if strict and failed:
            raise NaxError("could not read the zones ("
                           + "; ".join(f"{k}: {v}" for k, v in failed.items()) + ")")
        out, routes, zs = {}, got["Routes"], got["Zones"]
        for name in sorted(set(routes) | set(zs), key=_zone_order):
            out[name] = {
                "name": self._label(zs.get(name)) or self._label(routes.get(name)),
                "source": (routes.get(name) or {}).get("AudioSource", ""),
                # Decays: stays true for about six seconds after a source is
                # removed, so it proves presence and never absence.
                "signal": (zs.get(name) or {}).get("IsSignalDetected"),
                # Reported because it is the difference between an announcement
                # that failed and one that played into a room turned down: at
                # 14 Malke the Kitchen sat at 170 against the Living Room's 600
                # and was simply inaudible, while every route and signal check
                # said the page had worked.
                "volume": ((zs.get(name) or {}).get("ZoneAudio") or {}).get("Volume"),
                # A bussed zone follows another. The Snug at 110 Roosevelt is
                # Zone5 (stereo) + Zone6 (bridged mono), BusId 1 on both, and
                # routing Zone5 alone switches, raises and restores Zone6 with
                # it - measured with a silent clip, 2026-09-14. One room.
                "bussed": bool((zs.get(name) or {}).get("IsBussed")),
                "bus": (zs.get(name) or {}).get("BusId"),
                "config": (zs.get(name) or {}).get("ZoneConfiguration"),
            }
        return mark_buses(out)

    # -- the receive slot a zone listens on --------------------------------
    def rx_streams(self, strict=False):
        """Every receive slot, {StreamNN: {...}}.

        `strict` raises when the amplifier could not be read. Anything that
        decides whether a slot is FREE must read strictly: a failed read that
        comes back as "no slots" makes every slot look empty, and the next step
        would take over somebody else's.
        """
        try:
            dev = self.get("NaxAudio")
        except Exception as e:
            if strict:
                raise NaxError(f"could not read the receive slots ({type(e).__name__}: {e}) "
                               f"- not touching them") from e
            return {}
        try:
            return dev["NaxAudio"]["NaxRx"]["NaxRxStreams"] or {}
        except (KeyError, TypeError) as e:
            # A reply that parsed but has the wrong shape is a failed read
            # too: other firmware, or a session in a bad state. Read as "no
            # slots" it is exactly the takeover `strict` exists to prevent.
            # (A NaxRxStreams that is present and empty is a real answer.)
            if strict:
                raise NaxError(f"the receive slots came back without NaxRxStreams "
                               f"({type(e).__name__}: {e}) - not touching them") from e
            return {}

    @staticmethod
    def slot_of(zone):
        """`Zone4` -> `Stream04`. `Stream0N` belongs to `ZoneN`."""
        try:
            n = int(str(zone).replace("Zone", "").strip())
        except ValueError as e:
            raise NaxError(f"{zone} is not a zone name") from e
        return f"Stream{n:02d}"

    def rx_known(self, zone, session_name):
        """Already confirmed on this session, so `ensure_rx` will cost nothing."""
        try:
            return (self.slot_of(zone), session_name) in self._rx_ok
        except NaxError:
            return False

    def ensure_rx(self, zone, session_name, address, port=5004, streams=None):
        """Point ZoneN's receive slot at our stream, if it is ours to point.

        `Stream0N` belongs to `ZoneN` - Stream04 is Deck. A slot that is empty,
        or that already names our session, is ours to set.

        🔴 A slot carrying **somebody else's** session is left alone and the
        zone is refused. These are client houses, and a NAX receive slot can
        be a real distribution path between amplifiers; quietly stealing one
        would take a room off the air somewhere else in the building and the
        symptom would appear nowhere near this code.

        Returns True if it wrote, False if it was already right, and raises if
        the slot belongs to something else (`SlotTaken`) or could not be read.

        `streams` is a `rx_streams()` read the caller already made, so a pass
        over eight zones costs one REST round trip rather than eight. At 69
        Phillips (2026-10-13) the per-zone read plus its settle was 2.5 s a
        zone, paid in the middle of the house's first page.
        """
        slot = self.slot_of(zone)
        # Already confirmed on this session. The slot does not wander while we
        # hold the connection, and re-checking costs a REST round trip on every
        # single page - or a whole SETTLE, if it decides to write again.
        if (slot, session_name) in self._rx_ok:
            return False
        cur = (streams if streams is not None else self.rx_streams(strict=True)).get(slot) or {}
        mine = (cur.get("SessionNameRequested") or cur.get("SessionName") or "").strip()
        if mine == session_name:
            # The name alone is not "right". Stream06 at 14 Malke carried our
            # session name and port 4570 - a leftover from an early sender - and
            # this check waved it through for days while the Social Room heard
            # nothing. Address and port have to agree too. Corrected in place
            # by a plain re-subscribe; never by stopping the slot (see subscribe).
            wrong = (str(cur.get("PortRequested") or "") != str(port)
                     or (cur.get("NetworkAddressRequested") or "") != address)
            if not wrong:
                self._rx_ok.add((slot, session_name))
                return False
            self.log(f"[nax] {self.host} {zone}: receive slot names our session "
                     f"but asks for {cur.get('NetworkAddressRequested')}:"
                     f"{cur.get('PortRequested')} - correcting to {address}:{port}")
        if mine:
            raise SlotTaken(
                f"{zone}'s receive slot is already carrying {mine!r} - "
                f"leaving it alone rather than taking it over")
        self.subscribe(slot, session_name, address, port)
        time.sleep(SETTLE)
        self._rx_ok.add((slot, session_name))
        return True

    # -- the WebSocket, for writing ---------------------------------------
    def open(self):
        self.ws = websocket.create_connection(
            f"wss://{self.host}/websockify",
            sslopt={"cert_reqs": ssl.CERT_NONE},
            header=[f"Cookie: {self._cookie_header()}",
                    f"Referer: https://{self.host}/"],
            origin=f"https://{self.host}", timeout=20)
        self._stop = False
        self.closed_why = None
        self.opened_at = time.time()
        self._thread = threading.Thread(target=self._listen, daemon=True,
                                        name=f"nax-listen-{self.host}")
        self._thread.start()
        return self

    def alive(self):
        """Is this session still usable, without asking the amplifier?

        The websocket is the fragile half. Until 0.15.0 `_listen` swallowed
        every exception, so a dropped socket raised nowhere - it just stopped
        updating `routes`, and the next write failed with "socket is already
        closed". Now the listener notices the close within its 0.6 s read
        timeout and says so here.
        """
        if self._stop or self.ws is None or self.closed_why is not None:
            return False
        if self._thread is not None and not self._thread.is_alive():
            return False
        return bool(getattr(self.ws, "connected", False))

    def _listen(self):
        """Read pushes until the socket closes or we close it.

        🔴 **It stops when the socket does.** At 69 Phillips (2026-10-13) the
        socket closed twelve seconds into the house's first page and the next
        write said "socket is already closed". The old loop was `except
        Exception: pass` around `recv()`: once websocket-client has dropped its
        socket every `recv()` raises at once, so the listener spun flat out -
        holding the interpreter against the very thread trying to finish the
        page - and told nobody. Now a close is recorded (with the amplifier's
        close code, if it sent one), logged once, and `alive()` goes false, so
        the page reconnects before it writes rather than after it fails.
        """
        ws = self.ws
        ws.settimeout(0.6)
        while not self._stop:
            try:
                opcode, data = ws.recv_data(control_frame=True)
            except Exception as e:
                if self._stop:
                    break
                if type(e).__name__ == "WebSocketTimeoutException":
                    continue
                if not getattr(ws, "connected", False) or getattr(ws, "sock", True) is None:
                    self._closed(f"{type(e).__name__}: {e}")
                    break
                # A frame that would not parse, on a socket still open: carry
                # on, but never in a tight loop.
                time.sleep(0.05)
                continue
            if opcode == 8:
                # A close frame. websocket-client has already answered it; the
                # code and reason are the only account of why we will get.
                raw = data or b""
                code = int.from_bytes(raw[:2], "big") if len(raw) >= 2 else None
                reason = raw[2:].decode("utf-8", "replace") if len(raw) > 2 else ""
                self._closed(f"the amplifier sent close {code} {reason!r}".rstrip())
                break
            if opcode not in (1, 2):
                continue        # ping/pong; the library has answered a ping
            self._take(data)

    def _closed(self, why):
        if self.closed_why is not None or self._stop:
            return
        age = time.time() - self.opened_at if self.opened_at else 0.0
        self.closed_why = why
        self.log(f"[nax] {self.host}: websocket closed {age:.1f}s after it opened ({why})")

    def _take(self, raw):
        """One pushed frame: keep what it says about routes."""
        try:
            text = (raw if isinstance(raw, str)
                    else raw.decode("utf-8", "replace")).strip()
            if not text:
                return
            routes = (json.loads(text).get("Device", {})
                      .get("AvMatrixRouting", {}).get("Routes", {}))
        except Exception:
            return
        for zone, v in routes.items():
            if not isinstance(v, dict):
                continue
            if not v:
                # An empty object is the device clearing the route,
                # which is a step in the dance rather than a failure.
                self.routes[zone] = ""
            elif "AudioSource" in v:
                self.routes[zone] = v["AudioSource"] or ""
            # 🔴 Anything else is an update *about* the zone - volume,
            # signal, mute - that says nothing about its source, and
            # must not be read as one.
            #
            # This used to be `v.get("AudioSource", "")`, which turned
            # every one of those into "the route is empty". Clearing a
            # zone was then confirmed by the next unrelated push about
            # it, whether or not the clear had landed. Five rooms at
            # 14 Malke sat on a silent input for a day because of this
            # one default, and the log said "restored" for every one
            # of them.

    def seed_routes(self, listing):
        """What a REST read says each zone is on, for zones no push has
        mentioned yet. Pushes win; this only fills gaps.

        A fresh session has heard nothing, and `_holding` reads nothing as
        "empty" - so clearing a zone on a brand-new connection was confirmed
        before the amplifier had said a word. That is fine for the session a
        page has held for seconds, and wrong for one built to retry a restore.
        """
        for zone, info in (listing or {}).items():
            self.routes.setdefault(zone, (info or {}).get("source") or "")

    def _send(self, obj):
        with self._wlock:
            self.ws.send(json.dumps(obj))

    def route(self, zone, source):
        self._send({"Device": {"AvMatrixRouting": {"Routes": {zone: {"AudioSource": source}}}}})

    def set_volume(self, zone, level):
        """Set one zone's volume, on the amplifier's own 0-900 scale.

        Not dB: the level a given step lands on depends on the zone's speaker
        power and impedance settings, so 600 is -12.0 dB in 14 Malke's Kitchen
        and -25.9 dB in its Living Room. Compare and set within a zone, never
        across zones.
        """
        self._send({"Device": {"ZoneOutputs": {"Zones": {
            zone: {"ZoneAudio": {"Volume": int(level)}}}}}})

    def route_sticky(self, zone, source, attempts=ATTEMPTS, settle=SETTLE):
        """Write one route until it holds. Returns the writes taken, or None."""
        return self.route_all({zone: source}, attempts, settle).get(zone)

    def _holding(self, zone, source):
        """Is this zone on this source? A zone we have never heard about
        counts as empty, which is the only sane reading of silence when the
        thing being asked is whether it has been cleared."""
        return (self.routes.get(zone) or "") == (source or "")

    def route_all(self, wanted, attempts=ATTEMPTS, settle=SETTLE, on_landed=None, stop=None):
        """Write a set of routes until they hold. {zone: source} in,
        {zone: writes or None} out.

        🔴 **One zone at a time.** This was briefly batched - write every
        zone, then let the two 1.5 s settles overlap - which would have taken
        a whole-house page from fifty seconds of switching down to eight. The
        amplifier does not allow it: given several route writes at once it
        acts on one and silently ignores the rest, so a repair run cleared
        exactly one room per attempt and reported all of them cleared.

        That was an assumption about the device, not a measurement of it, and
        it is the second time this file has been wrong that way. The original
        session's rule stands: write, wait, confirm, and do it per zone.

        Clearing a zone is confirmed like anything else. It used to be written
        once and not waited on, and that is how five rooms at 14 Malke ended
        up stuck on the announcement input with the log saying "restored" for
        every one of them. An empty route is a route.

        `stop` (a callable) ends it early - between zones and during the watch -
        when the page it is routing for is already over. Whatever it had written
        is restored by the caller anyway; nothing new is written after it.
        """
        stop = stop or (lambda: False)
        # Pipelined, not batched. The amplifier acts on ONE route write at a
        # time and silently drops others sent in the same instant - that was
        # measured, and it is why the old loop did every zone in full before
        # touching the next: write, land, watch 1.5 s, next zone. Five zones
        # took eight seconds before a page could make a sound.
        #
        # What the amplifier does NOT need is a full watch per zone. A write
        # can go out the moment the previous one has landed (about a tenth of
        # a second), and one settle watch at the end covers every zone at
        # once: the thing being watched for - the device quietly clearing a
        # route after its first write - shows up within the same 1.5 s for all
        # of them. Anything cleared is written again and the watch repeats.
        # Five zones: about two seconds. One zone: exactly what it was.
        out = {zone: None for zone in wanted}
        writes = {zone: 0 for zone in wanted}
        pending = list(wanted)
        for _ in range(attempts):
            if not pending:
                break
            landed = []
            for zone in pending:
                if stop():
                    return out
                self.route(zone, wanted[zone])
                writes[zone] += 1
                if self._lands(zone, wanted[zone], settle):
                    landed.append(zone)
                    # The moment the route is in, before the settle: an idle
                    # zone has just turned on and taken its DefaultVolume, and
                    # the caller has ~1.5 s before audio starts to put the
                    # level back. After the settle would be too late for the
                    # first half-second of a chime.
                    if on_landed is not None:
                        try:
                            on_landed(zone)
                        except Exception as e:
                            self.log(f"[nax] {self.host} {zone}: on_landed failed ({e})")
            # One watch for everything that landed this round.
            deadline = time.time() + settle
            cleared = set()
            while time.time() < deadline and not stop():
                for zone in landed:
                    if zone not in cleared and not self._holding(zone, wanted[zone]):
                        cleared.add(zone)
                time.sleep(0.05)
            for zone in landed:
                if zone not in cleared and self._holding(zone, wanted[zone]):
                    out[zone] = writes[zone]
            pending = [z for z in pending if out[z] is None]
        return out

    def _lands(self, zone, source, window, step=0.05):
        """Wait up to `window` for a route to appear. Did it?

        Records WHEN it appeared. The zone starts fading in on its new source
        at that instant, not when we finish satisfying ourselves that the route
        stuck - and those are two names for the same 1.5 seconds. A caller that
        waits out the watch and THEN waits a lead pays for it twice.
        """
        deadline = time.time() + window
        while not self._holding(zone, source):
            if time.time() >= deadline:
                return False
            time.sleep(step)
        self.landed[zone] = time.time()
        return True

    def _stays(self, zone, source, window, step=0.05):
        """Watch a route that has landed for `window`. Did it survive?"""
        deadline = time.time() + window
        while time.time() < deadline:
            if not self._holding(zone, source):
                return False
            time.sleep(step)
        return self._holding(zone, source)

    def subscribe(self, stream, session_name, address, port=5004):
        """Point a zone's Rx slot at our announced stream.

        Over the WebSocket only - the REST equivalent returns 200 and does
        nothing at all.

        Never write StopRequested to a slot, here or anywhere. On 2026-09-08 a
        stop/clear/start "repair" of one slot made the amplifier LEAVE the
        multicast group, and the switch's IGMP snooping never honoured its
        re-join: every slot sat on "Connecting" with no packets, the house was
        silent for pages, and nothing on the amplifier or the sender brought it
        back. Only a different group - a genuinely new join - did. A slot is
        corrected by writing the right values over it, and nothing else.
        """
        self._send({"Device": {"NaxAudio": {"NaxRx": {"NaxRxStreams": {
            stream: {"SessionNameRequested": session_name,
                     "NetworkAddressRequested": address,
                     "PortRequested": int(port),
                     "StartRequested": True}}}}}})

    def close(self):
        self._stop = True
        # Give the listener its read timeout to notice and stop - but only if
        # it is still running. It was a flat 0.8 s sleep, paid by every
        # reconnect on a page's critical path even when the listener had
        # already exited on a dead socket.
        t = self._thread
        if t is not None and t.is_alive() and t is not threading.current_thread():
            t.join(0.8)
        try:
            if self.ws:
                self.ws.close()
        except Exception:
            pass


def announce(host, user, password, zones, play, *, source=SOURCE,
             restore=None, log=print, session_name="", address=""):
    """One amplifier. Kept as the simple case; `announce_many` does the rest."""
    return announce_many({host: {"user": user, "password": password}},
                         {host: zones}, play, source=source, log=log,
                         session_name=session_name, address=address,
                         restore={host: restore} if restore else None)


class Pool:
    """One logged-in, websocket-open session per amplifier, kept between pages.

    A page used to spend about a second before routing anything: an HTTPS
    login, a websocket handshake, a zones read and a receive-slot check, every
    time, against an amplifier this process already holds a permanent stream
    to. The login and the socket are the parts that do not need repeating.

    A pooled session is dropped rather than repaired the moment it looks wrong.
    Reconnecting costs a third of a second; guessing wrong about a stale
    session costs a page, and this add-on's worst failures have all been rooms
    left silent by something that carried on as if it had worked.

    🔴 **Except from under a page.** A session a page is using is `hold`-ed,
    and `drop` from anybody else - the tile's zone list after a failed read,
    say - leaves it alone while it still works. The page itself drops with
    `force` when it has seen the socket die. This is a guard, not the cause
    of 69 Phillips: in 0.14.2 `drop` only ran after `get` itself had failed,
    so it could never have closed a live session a page held. "socket is
    already closed" there means websocket-client had read EOF - the amplifier
    (or the network) closed the TCP connection.

    Connecting is per amplifier, not under one lock: seven amplifiers logging
    in one after another is the sequential page this version exists to end.
    """

    def __init__(self, log=print):
        self._conns = {}
        self._held = {}           # host -> pages using its session right now
        self._hosts = {}          # host -> the lock its connecting is done under
        self._lock = threading.Lock()
        self.log = log

    def _host_lock(self, host):
        with self._lock:
            return self._hosts.setdefault(host, threading.Lock())

    def get(self, host, cfg):
        with self._host_lock(host):
            with self._lock:
                nax = self._conns.get(host)
                if nax is not None:
                    if nax.alive():
                        return nax
                    self._conns.pop(host, None)
            if nax is not None:
                self.log(f"[nax] {host}: pooled session went away, reconnecting")
                self._retire(nax)
            nax = Nax(host, cfg.get("user") or "admin",
                      cfg.get("password") or "", log=self.log)
            nax.login()
            nax.open()
            with self._lock:
                self._conns[host] = nax
            return nax

    def hold(self, host):
        with self._lock:
            self._held[host] = self._held.get(host, 0) + 1

    def release(self, host):
        with self._lock:
            n = self._held.get(host, 0) - 1
            if n > 0:
                self._held[host] = n
            else:
                self._held.pop(host, None)

    def held(self, host):
        with self._lock:
            return self._held.get(host, 0) > 0

    def drop(self, host, force=False):
        """Throw a session away - call this whenever one has misbehaved."""
        with self._lock:
            nax = self._conns.get(host)
            if nax is None:
                return
            if not force and self._held.get(host) and nax.alive():
                self.log(f"[nax] {host}: a page is using this session - not dropping it")
                return
            self._conns.pop(host, None)
        self._retire(nax)

    def reconnect(self, host, cfg):
        """A fresh login and socket, whatever state the old one was in."""
        self.drop(host, force=True)
        return self.get(host, cfg)

    @staticmethod
    def _retire(nax):
        # Outside the pool's lock: `close` waits most of a second for the
        # listener, and every other amplifier's page would wait with it.
        try:
            nax.close()
        except Exception:
            pass

    def close(self):
        with self._lock:
            conns, self._conns = list(self._conns.values()), {}
        for nax in conns:
            self._retire(nax)


# Pages in progress in this process. `prewarm` waits while this is non-zero, so
# a page never shares an amplifier's socket with a background slot check.
_pages = [0]
_pages_lock = threading.Lock()


def paging():
    return _pages[0] > 0


class Journal:
    """What a page changed and has not yet put back - on disk, so a restart
    can finish a restore that a crash, or an amplifier that stopped answering,
    interrupted.

    {"host:zone": {"source": what it was on, "volume": what it was at,
                   "set": the level we last wrote}}. An entry's parts are
    removed as each is put back; an empty entry is removed.

    69 Phillips (2026-10-13) is why: five rooms were left at 700 after a page
    whose restore never ran, and nothing on the box remembered they had been
    300 and 500.
    """

    def __init__(self, path):
        self.path = path
        self._lock = threading.Lock()

    def _load(self):
        try:
            with open(self.path) as f:
                data = json.load(f)
            return data if isinstance(data, dict) else {}
        except (OSError, ValueError):
            return {}

    def _save(self, data):
        try:
            if not data:
                if os.path.exists(self.path):
                    os.unlink(self.path)
                return
            tmp = self.path + ".tmp"
            with open(tmp, "w") as f:
                json.dump(data, f)
            os.replace(tmp, self.path)
        except OSError:
            pass

    def note(self, host, zone, **fields):
        """Record a change. The FIRST original wins: a second page over a zone
        an earlier one never put back must not record our own leftovers as
        what the room had. `set` is always the latest."""
        with self._lock:
            data = self._load()
            entry = data.setdefault(f"{host}:{zone}", {})
            for k, v in fields.items():
                if k == "set" or k not in entry:
                    entry[k] = v
            self._save(data)

    def forget_stale(self, host, zone, source_now, volume_now, ours=SOURCE):
        """Drop the parts of an entry the room has since moved away from, so
        the page about to touch it records what it has NOW.

        "First original wins" protects the original from our own leftovers -
        and only those. If the house has chosen something since (the zone is
        on another source, or at a level other than the one we wrote), the
        old original is stale: `recover` would put back a source and level
        the client has since replaced. Same test as `recover`'s "still ours".
        """
        with self._lock:
            data = self._load()
            key = f"{host}:{zone}"
            entry = data.get(key)
            if entry is None:
                return
            if "source" in entry and source_now != ours:
                entry.pop("source", None)
            if "volume" in entry and (volume_now is None or volume_now != entry.get("set")):
                entry.pop("volume", None)
                entry.pop("set", None)
            if not entry:
                data.pop(key, None)
            self._save(data)

    def done(self, host, zone, *parts):
        with self._lock:
            data = self._load()
            key = f"{host}:{zone}"
            entry = data.get(key)
            if entry is None:
                return
            for p in parts:
                entry.pop(p, None)
                if p == "volume":
                    entry.pop("set", None)
            if not entry:
                data.pop(key, None)
            self._save(data)

    def entries(self):
        with self._lock:
            return self._load()


def announce_many(amps, targets, play, *, source=SOURCE, restore=None,
                  log=print, session_name="", address="", floor=None,
                  ready=None, pool=None, port=5004, home=None, cap=None,
                  journal=None):
    """Switch zones on one or more amplifiers, play once, put them all back.

    `amps` is {host: {"user":…, "password":…}}; `targets` is {host: [zones]}.

    One `play()` for all of them, because there is only one stream: AES67 is
    multicast, so every amplifier on the LAN hears the same audio at the same
    moment. Paging four rooms on two amplifiers is four routes and one voice,
    not two announcements that would arrive a second apart and echo.

    🔴 **Every amplifier at once.** Each amplifier is prepared and routed in its
    own thread; within one amplifier the zones still go one at a time, which is
    the device's rule (see `route_all`), not ours. Until 0.15.0 amplifiers went
    one after another too, and at 69 Phillips (2026-10-13, seven DM-NAX, 46
    zones) the first amplifier was still pointing receive slots twelve seconds
    in when its socket closed; the other six were never reached.

    `cap` is how long the page waits for the slowest amplifier, in seconds
    from the start, once at least one has landed. When it passes, `play()`
    starts and the amplifiers still routing join late - their rooms hear the
    rest of the page rather than everybody waiting for them. None waits for
    all of them. "Landed" is every zone's route seen to bind, not the
    amplifier's thread finishing: its settle watch and volume read-back run
    alongside the audio (see `routed`).

    Restoring happens in a `finally`, because a zone left on a silent AES67
    input is a room whose music never comes back - and that is a worse failure
    than the announcement not playing at all. Every amplifier is restored even
    if an earlier one threw on the way in, all at once, and an amplifier whose
    socket has died is reconnected and tried again rather than skipped (see
    `restore_host`). `journal` (a `Journal`) remembers what is not yet back.

    `floor` raises any zone quieter than it for the duration, and puts it back
    afterwards. A page that is only audible in the rooms somebody happened to
    leave turned up is not a page; 14 Malke's Kitchen sat 25 dB below its Living
    Room and heard nothing at all while every other check passed. It only ever
    raises - a room already playing louder is left where it is, because turning
    music DOWN to announce over it is the one thing nobody asks for.
    """
    conns = {}
    before = {}          # (host, zone) -> the source it had
    volumes = {}         # (host, zone) -> the volume it had, if we raised it
    taken = {}           # host -> zones actually bound to us
    contested = {}       # "host:zone" -> times its route was taken back mid-clip
    resume = {}          # (host, zone) -> the Crestron Home source id to resume
    refused = []
    switched = set()     # (host, zone) a route write to `source` may have gone to
    errors = {}          # host -> why it could not be prepared at all
    wrote = {}           # (host, zone) -> the volume we last wrote, for the read-back
    bound_at = {}        # (host, zone) -> when its route landed, on THIS page
    # Set per amplifier once every zone it is taking has landed (or it has
    # given up). Playback waits on these, not on the prepare threads: a thread
    # still has its settle watch and a volume read-back to do after its last
    # route lands, and those run alongside the audio rather than ahead of it.
    routed = {h: threading.Event() for h in targets}
    # Every structure above is shared by one thread per amplifier, the guard
    # and the restore. They are touched under this lock and nowhere else.
    state = threading.Lock()
    # Set the moment the page is over. A thread still preparing sees it before
    # anything audible and stops, so nothing is switched after the restore
    # has started.
    closing = threading.Event()
    # What Crestron Home thinks is playing, read ONCE per page. The amplifier is
    # not what pauses the music - the processor is: it sees the zone leave its
    # player and pauses it, and only the processor's own Play brings it back.
    # A zone is matched to its media room by NAME, which is the one thing the
    # amplifier and the processor were both configured with from the same list.
    # Read in a thread while the amplifier sessions are fetched and the zones
    # are read, and joined just before each amplifier's first route write - it
    # has to describe the house BEFORE the page touches anything.
    playing_rooms = {}
    reader = None
    if home is not None:
        def read_home():
            try:
                playing_rooms.update(home.rooms_playing())
            except Exception as e:
                log(f"[crpc] could not read what Crestron Home is playing ({e}); "
                    f"music will not be resumed after this page")
        reader = threading.Thread(target=read_home, daemon=True, name="crpc-read")
        reader.start()

    def level_for(zone, host):
        # `floor` is one level for every zone, or {"host:zone": level} with a
        # "*" default - the settings page sets one per room. The host is an
        # argument now: it was a shared "current host" cell, which one thread
        # per amplifier would have read for each other.
        if not isinstance(floor, dict):
            return floor
        return floor.get(f"{host}:{zone}", floor.get("*"))

    def connect(host, cfg):
        if pool is not None:
            return pool.get(host, cfg)
        nax = Nax(host, cfg.get("user") or "admin", cfg.get("password") or "", log=log)
        nax.login()
        nax.open()
        return nax

    def reconnect(host):
        """A fresh session for `host`, its routes seeded from a REST read so a
        clear is not confirmed by silence (see `seed_routes`)."""
        cfg = amps.get(host) or {}
        with state:
            old = conns.get(host)
        if pool is not None:
            nax = pool.reconnect(host, cfg)
        else:
            if old is not None:
                try:
                    old.close()
                except Exception:
                    pass
            nax = connect(host, cfg)
        try:
            # Strict: a failed read used to come back empty, seed nothing, and
            # leave every clear on this session confirmed by silence - the very
            # thing the seeding is for. A session we cannot seed is not used.
            nax.seed_routes(nax.zones(strict=True))
        except Exception as e:
            log(f"[nax] {host}: reconnected, but could not read the routes back ({e}) - "
                f"not using that session")
            raise
        with state:
            conns[host] = nax
        return nax

    def current(host):
        with state:
            return conns.get(host)

    def raise_to(host, zone, level, had):
        """Write a volume for the page, recording what to put back first.
        Under the lock, so it either happens before the restore takes its list
        or not at all."""
        with state:
            if closing.is_set():
                return False
            # 🔴 A volume we could not read is never raised. Nothing could put
            # it back, and the room may already be louder than `level` - this
            # would be turning a loud room down.
            if had is None:
                log(f"[nax] {host} {zone}: its volume could not be read - leaving it "
                    f"where it is")
                return False
            volumes.setdefault((host, zone), had)
            wrote[(host, zone)] = int(level)
            if journal is not None:
                journal.note(host, zone, volume=had, set=int(level))
            conns[host].set_volume(zone, level)
        return True

    def prepare(host, zones):
        """One amplifier, start to bound. Runs in its own thread."""
        cfg = amps.get(host) or {}
        try:
            # Read every zone once, not once per zone. Each read is two HTTPS
            # round trips to the amplifier, and paging the whole house would
            # otherwise spend a dozen of them standing there before anybody
            # heard anything.
            #
            # This doubles as the health check on a pooled session: it is the
            # first REST call of the page, so an expired login fails HERE,
            # before anything has been routed, and can be retried on a fresh
            # connection at the cost of a third of a second. `alive()` cannot
            # see that - it only knows about the websocket.
            for attempt in (1, 2):
                try:
                    nax = connect(host, cfg)
                    # Strict: an empty listing would record "" as what every
                    # room was playing, and the restore would clear its music.
                    was_all = nax.zones(strict=True)
                    break
                except Exception as e:
                    if pool is None or attempt == 2:
                        raise
                    log(f"[nax] {host}: pooled session did not answer "
                        f"({type(e).__name__}), rebuilding it")
                    pool.drop(host, force=True)
            with state:
                conns[host] = nax
            nax.seed_routes(was_all)
            # A bussed partner is driven by its primary - routing the primary
            # switched, raised and restored the partner (measured). Page the
            # primary once, whichever of the two was asked for.
            folded = list(dict.fromkeys((was_all.get(z) or {}).get("bus_of") or z for z in zones))
            if folded != list(zones):
                log(f"[nax] {host}: {list(zones)} -> {folded} (bussed)")
            zones[:] = folded

            # 1. Receive slots. Nothing about a room changes here, and after
            # the background warm-up (`prewarm`) this is normally free.
            taking = []
            streams = None
            for zone in zones:
                if closing.is_set():
                    return
                if not session_name:
                    taking.append(zone)
                    continue
                for attempt in (1, 2):
                    try:
                        if streams is None and not nax.rx_known(zone, session_name):
                            streams = nax.rx_streams(strict=True)
                        # A zone whose receive slot belongs to something else
                        # is refused rather than taken over - see `ensure_rx`.
                        # The page still goes ahead everywhere else, because
                        # one awkward room should not silence the whole house.
                        if nax.ensure_rx(zone, session_name, address, port, streams=streams):
                            log(f"[nax] {host} {zone}: receive slot pointed at "
                                f"{session_name!r}")
                        taking.append(zone)
                        break
                    except NaxError as e:
                        log(f"[nax] {host} {zone}: {e}")
                        with state:
                            refused.append(f"{host}:{zone}")
                        break
                    except Exception as e:
                        if attempt == 2 or nax.alive():
                            raise
                        log(f"[nax] {host}: socket closed while pointing {zone}'s receive "
                            f"slot ({type(e).__name__}); reconnecting")
                        nax = reconnect(host)
                        streams = None

            # 2. The socket, checked before anything a room would notice. The
            # slot pass can take seconds on an amplifier that was never
            # warmed; a socket that died in that time is replaced now rather
            # than discovered by the first route write.
            if not nax.alive():
                log(f"[nax] {host}: socket closed while preparing ({nax.closed_why}); reconnecting "
                    f"before routing")
                nax = reconnect(host)

            with state:
                if closing.is_set():
                    return
                for zone in taking:
                    was = ((restore or {}).get(host) or {}).get(zone)
                    if was is None:
                        was = was_all.get(zone, {}).get("source", "")
                    # 🔴 Never restore a zone to our own announcement input.
                    #
                    # If a zone is already on `Aes67` when a page starts, it is
                    # not because the house was listening to us - it is because
                    # an earlier page failed to put it back. Recording that as
                    # "what it was playing" makes the fault permanent: every
                    # page from then on faithfully restores the room to a silent
                    # input, and the log says "restored" each time. Five rooms
                    # at 14 Malke were stuck this way, cemented by every
                    # announcement after the first.
                    if was == source:
                        log(f"[nax] {host} {zone}: was already on {source} - an "
                            f"earlier announcement did not put it back; clearing "
                            f"it instead of restoring it")
                        was = ""
                    before[(host, zone)] = was
                    if journal is not None:
                        now = was_all.get(zone) or {}
                        journal.forget_stale(host, zone, now.get("source", ""),
                                             now.get("volume"), source)
                        journal.note(host, zone, source=was)
            for zone in taking:
                level = level_for(zone, host)
                if level:
                    had = (was_all.get(zone) or {}).get("volume")
                    if had is not None and had < level:
                        if raise_to(host, zone, level, had):
                            log(f"[nax] {host} {zone}: volume {had} -> {level} "
                                f"for the announcement")
            # The processor's answer is needed from here on - and must predate
            # the first route write. Joining a thread that has finished is free.
            if reader is not None:
                reader.join(8.0)
            with state:
                for zone in taking:
                    # Only rooms whose music is PLAYING now are resumed
                    # afterwards. A room that was already paused stays paused:
                    # this never starts music, it only puts back what the page
                    # interrupted.
                    room = (was_all.get(zone, {}).get("name") or "").strip().lower()
                    if room in playing_rooms:
                        resume[(host, zone)] = playing_rooms[room]
                if closing.is_set():
                    return
                # Recorded BEFORE the first write, so the restore covers a zone
                # whose write went out even if its confirmation never came.
                switched.update((host, z) for z in taking)

            def relevel(zone):
                # The zone has just turned on (if it was idle) and reset to its
                # default. Back to the announcement level - or to what the room
                # had if that was louder: a loud room is left loud, never
                # turned down for a page.
                level = level_for(zone, host)
                if not level:
                    return
                had = (was_all.get(zone) or {}).get("volume")
                target = max(level, had or 0)
                if raise_to(host, zone, target, had):
                    log(f"[nax] {host} {zone}: route landed, volume set to {target} "
                        f"(was {had}, announce at {level})")

            def landed(zone):
                with state:
                    bound_at[(host, zone)] = time.time()
                    every = all((host, z) in bound_at for z in taking)
                try:
                    relevel(zone)
                finally:
                    # After the level, so audio never starts in a room still
                    # at its default.
                    if every:
                        routed[host].set()

            # All of this amplifier's zones together - see `route_all`. A socket
            # that dies mid-way is replaced and the zones not yet on our input
            # are written again; the ones that are count as bound.
            wanted = {z: source for z in taking}
            if not wanted:
                routed[host].set()
            result = {}
            for attempt in (1, 2):
                try:
                    result.update(nax.route_all(wanted, on_landed=landed, stop=closing.is_set))
                    break
                except Exception as e:
                    if attempt == 2 or nax.alive() or closing.is_set():
                        raise
                    log(f"[nax] {host}: socket closed while routing ({type(e).__name__}: "
                        f"{e}); reconnecting")
                    nax = reconnect(host)
                    for z in list(wanted):
                        if nax._holding(z, source):
                            result[z] = 0
                            wanted.pop(z)
                            if (host, z) not in bound_at:
                                landed(z)
            for zone in taking:
                n = result.get(zone)
                if n is None:
                    log(f"[nax] {host} {zone}: {source} would not bind")
                    with state:
                        refused.append(f"{host}:{zone}")
                else:
                    log(f"[nax] {host} {zone}: {source} bound after {n} "
                        f"write(s), was {before[(host, zone)]!r}")
                    with state:
                        taken.setdefault(host, []).append(zone)
            # 🔴 The volume again, AFTER the route. A zone that was idle turns
            # on when a source is routed to it and takes its DefaultVolume as
            # it does - wiping the level set a second earlier. Measured at 110
            # Roosevelt (2026-09-14): The Snug set to 700, route lands, the
            # amplifier reads 300. A zone already playing was already on, so
            # its level stuck, which is why "the volume changes when music is
            # playing but not when nothing is" was exactly the symptom.
            with state:
                mine = list(taken.get(host, []))
            if not closing.is_set() and any(level_for(z, host) for z in mine):
                try:
                    now_all = nax.zones(strict=True)
                except Exception as e:
                    log(f"[nax] {host}: could not read the volumes back after routing ({e})")
                    now_all = {}
                for zone in mine:
                    level = level_for(zone, host)
                    cur = (now_all.get(zone) or {}).get("volume")
                    if level and cur is not None and cur < level:
                        if raise_to(host, zone, level, (was_all.get(zone) or {}).get("volume")):
                            log(f"[nax] {host} {zone}: volume fell to {cur} as the route "
                                f"landed (the zone's default) -> {level} again")
        except Exception as e:
            why = f"{type(e).__name__}: {e}"
            log(f"[nax] {host}: could not prepare this amplifier ({why})")
            with state:
                errors[host] = why
                for z in zones:
                    key = f"{host}:{z}"
                    if key not in refused and z not in taken.get(host, []):
                        refused.append(key)

    def prepare_then_signal(host, zones):
        try:
            prepare(host, zones)
        finally:
            routed[host].set()

    def restore_host(host):
        """Put one amplifier back: routes, then music, then volumes.

        🔴 **Never skipped.** If the socket is closed or a write raises, the
        session is thrown away, a fresh login and socket are made, and the
        restore is tried again - `RESTORE_TRIES` times with a growing pause -
        before anything is reported as COULD NOT RESTORE. At 69 Phillips the
        restore met a closed socket, logged COULD NOT RESTORE for every zone
        in the same second, and left five rooms at 700.
        """
        with state:
            routes = {z: w for (h, z), w in before.items() if h == host and (h, z) in switched}
            never = [z for (h, z) in before if h == host and (h, z) not in switched]
            levels = {z: v for (h, z), v in volumes.items() if h == host}
            resumes = {z: sid for (h, z), sid in resume.items() if h == host}
        for zone in never:
            # Recorded, but the page ended before a route was written: there
            # is nothing to put back, and writing its own source again could
            # only interrupt whatever it is playing.
            log(f"[nax] {host} {zone}: never switched - left on {before[(host, zone)]!r}")
            if journal is not None:
                journal.done(host, zone, "source")
        refusals = 0

        def attempt_loop(what, step):
            nonlocal refusals
            nax = current(host)
            for n in range(1, RESTORE_TRIES + 1):
                try:
                    if nax is None or not nax.alive():
                        raise NaxError("its websocket is closed"
                                       + (f" ({nax.closed_why})" if nax is not None and nax.closed_why else ""))
                    if step(nax):
                        return True
                    # Answering, and still refusing: route_all has already
                    # made six writes a zone. One fresh session is worth a try;
                    # four would hold the house for a minute and a half.
                    refusals += 1
                    if refusals > 1:
                        return False
                except Exception as e:
                    log(f"[nax] {host}: {what}, try {n} of {RESTORE_TRIES} failed "
                        f"({type(e).__name__}: {e})")
                if n < RESTORE_TRIES:
                    time.sleep(RESTORE_BACKOFF[min(n - 1, len(RESTORE_BACKOFF) - 1)])
                    try:
                        nax = reconnect(host)
                        log(f"[nax] {host}: reconnected to restore")
                    except Exception as e:
                        log(f"[nax] {host}: could not reconnect ({type(e).__name__}: {e})")
                        nax = None
            return False

        pending = dict(routes)

        def put_routes(nax):
            for zone, n in nax.route_all(dict(pending)).items():
                if n is not None:
                    log(f"[nax] {host} {zone}: restored to {pending[zone]!r}")
                    pending.pop(zone, None)
                    if journal is not None:
                        journal.done(host, zone, "source")
            if not nax.alive():
                raise NaxError("its websocket closed during the restore")
            return not pending

        if pending:
            attempt_loop("restoring routes", put_routes)
        for zone, was in pending.items():
            # The failure worth shouting about: a room left on a silent input
            # is a room whose music never comes back.
            log(f"[nax] {host} {zone}: COULD NOT RESTORE to {was!r}")
        # Right after the routes, so the zone is back on its own input when
        # the music starts again; before the volume, so the room does not come
        # up to level in silence and then start. This is the call the Crestron
        # Home app makes when somebody presses Play - measured on the
        # processor's own log, and proven to bring the Deck back in 1.5 s.
        for zone, source_id in resumes.items():
            try:
                home.play(source_id)
                log(f"[crpc] {host} {zone}: asked Crestron Home to resume source {source_id}")
            except Exception as e:
                log(f"[crpc] {host} {zone}: COULD NOT RESUME source {source_id} ({e})")
        # After the routes, so the room is back on its own source before it
        # comes back up to its own level.
        vpending = dict(levels)

        with state:
            ours_at = {z: v for (h, z), v in wrote.items() if h == host}

        def put_volumes(nax):
            for zone, had in list(vpending.items()):
                nax.set_volume(zone, had)
            if not nax.alive():
                raise NaxError("its websocket closed during the volume restore")
            # 🔴 Read back, not assumed. A send into a socket the amplifier has
            # just half-closed succeeds locally, and `alive()` only notices
            # within the listener's read timeout - so "still open after the
            # writes" proved nothing, and a zone left at 700 was crossed off
            # the journal as back at 300. Only the amplifier saying so counts.
            deadline = time.time() + SETTLE
            while True:
                now = nax.zones(strict=True)
                for zone, had in list(vpending.items()):
                    cur = (now.get(zone) or {}).get("volume")
                    if cur == had:
                        log(f"[nax] {host} {zone}: volume back to {had}")
                    elif cur is not None and cur != ours_at.get(zone):
                        # Neither ours nor theirs-before: somebody has turned
                        # it since. Theirs now - the same rule as `recover`.
                        log(f"[nax] {host} {zone}: volume is {cur}, not the {ours_at.get(zone)} "
                            f"we set - somebody has turned it; leaving it")
                    else:
                        continue
                    vpending.pop(zone, None)
                    if journal is not None:
                        journal.done(host, zone, "volume")
                if not vpending or time.time() >= deadline:
                    break
                time.sleep(0.25)
            if vpending:
                log(f"[nax] {host}: volume not yet back on "
                    + ", ".join(f"{z} (wanted {v})" for z, v in vpending.items()))
            return not vpending

        if vpending:
            refusals = 0
            attempt_loop("restoring volumes", put_volumes)
        for zone, had in vpending.items():
            log(f"[nax] {host} {zone}: COULD NOT RESTORE VOLUME to {had}")

    with _pages_lock:
        _pages[0] += 1
    threads = {}
    failure = None
    try:
        for host, zones in targets.items():
            if pool is not None:
                pool.hold(host)
            t = threading.Thread(target=prepare_then_signal, args=(host, list(zones)),
                                 name=f"nax-prep-{host}", daemon=True)
            threads[host] = t
            t.start()
        t0 = time.time()
        while True:
            # Routes landed, not threads finished: see `routed`.
            running = [h for h in threads if not routed[h].is_set()]
            if not running:
                break
            if cap is not None and time.time() - t0 >= cap and len(running) < len(threads):
                break
            time.sleep(0.02)
        if running:
            log(f"[nax] {', '.join(running)}: still preparing {time.time() - t0:.1f}s in - "
                f"starting without them; their rooms join when they land")
        with state:
            any_taken = bool(taken) or bool(bound_at)
            if not any_taken and not running and errors and not switched:
                failure = NaxError("; ".join(f"{h}: {e}" for h, e in errors.items()))

        def snapshot():
            with state:
                return {
                    "restored": {f"{h}:{z}": w for (h, z), w in before.items()},
                    "raised": {f"{h}:{z}": v for (h, z), v in volumes.items()},
                    "refused": list(refused),
                    "resumed": {f"{h}:{z}": sid for (h, z), sid in resume.items()},
                    "late": [h for h in threads if not routed[h].is_set()],
                    # When the LAST zone bound, on this page (a pooled
                    # session's `landed` remembers earlier pages too). Its
                    # lead has been running ever since - the caller should
                    # wait out whatever is left of it, not start a fresh one.
                    "landed_at": max(list(bound_at.values()) or [time.time()]),
                }

        def tell(snap):
            try:
                ready(snap)
            except Exception as e:
                log(f"[nax] ready callback raised, continuing: {e}")

        if any_taken or running:
            # Everything the caller needs to answer with is already known here:
            # which zones were taken, what each will go back to, which were
            # turned up. Handing it over now lets a caller reply as soon as the
            # room has heard the announcement instead of waiting out the
            # restore - which happens at the same moment either way.
            if ready:
                tell(snapshot())
            # Hold the routes while the clip plays. The watch in route_all only
            # proves a route survived its first settle; with the amplifier's
            # auto-route on, a zone's own streaming player can take it back at
            # any point after that, and the listener sees it within a tenth of
            # a second. Until now nobody looked, so a page cut off halfway
            # looked exactly like one that played.
            halt = threading.Event()
            told = [not ready]

            def hold():
                while not halt.wait(0.1):
                    # Playback can start before an amplifier's settle watch
                    # has given its verdict; once every prepare thread is done
                    # the caller hears again, so a zone that would not bind is
                    # in `refused` before anybody answers with it.
                    if not told[0] and not any(t.is_alive() for t in threads.values()):
                        told[0] = True
                        tell(snapshot())
                    with state:
                        pairs = [(h, z, conns.get(h)) for h, zs in taken.items() for z in zs]
                    for h, z, nax in pairs:
                        if nax and nax.alive() and not nax._holding(z, source):
                            key = f"{h}:{z}"
                            with state:
                                contested[key] = contested.get(key, 0) + 1
                            took = nax.routes.get(z) or "(nothing)"
                            log(f"[nax] {h} {z}: route taken back by {took!r} "
                                f"during the announcement - re-asserting {source}")
                            try:
                                nax.route(z, source)
                            except Exception as e:
                                log(f"[nax] {h} {z}: could not re-assert ({e})")

            guard = threading.Thread(target=hold, daemon=True)
            guard.start()
            try:
                play()
            finally:
                halt.set()
                guard.join(1.0)
    finally:
        # Nothing new is switched from here on; wait for anything still
        # preparing to notice, so the restore's list is the whole list.
        closing.set()

        def back(host):
            # Each amplifier waits for its OWN preparation only. One stuck
            # amplifier (a login timing out twice is forty seconds) used to
            # hold every other amplifier's rooms on the silent input with it.
            t = threads.get(host)
            if t is not None:
                t.join(PREP_JOIN)
                if t.is_alive():
                    log(f"[nax] {host}: still preparing {PREP_JOIN:.0f}s after the page ended - "
                        f"restoring what it recorded")
            restore_host(host)

        # Per amplifier and all at once, for the same reason as the way in -
        # and this half matters more. Every second here is a second somebody's
        # music is still off.
        backs = [threading.Thread(target=back, args=(h,), daemon=True,
                                  name=f"nax-restore-{h}") for h in threads]
        for b in backs:
            b.start()
        for b in backs:
            b.join()
        if pool is not None:
            for host in threads:
                pool.release(host)
        else:
            # A pooled session stays open - that is the whole point of it. Only
            # connections this call built are torn down here.
            for nax in conns.values():
                try:
                    nax.close()
                except Exception:
                    pass
        with _pages_lock:
            _pages[0] -= 1
    if failure is not None:
        raise failure
    return {"restored": {f"{h}:{z}": w for (h, z), w in before.items()},
            "raised": {f"{h}:{z}": v for (h, z), v in volumes.items()},
            "contested": contested,
            "resumed": {f"{h}:{z}": sid for (h, z), sid in resume.items()},
            "refused": refused}


def prewarm(amps, pool, session_name, address, port=5004, *, busy=None,
            log=print, wait=0.25):
    """Point every zone's receive slot at our stream, in the background, so a
    page never pays for it.

    A slot that was never pointed costs a write and a 1.5 s settle - per zone,
    and at 69 Phillips that was the first page's whole budget: 2.5 s a zone on
    the first amplifier, with six more waiting. Done here, at start-up, after
    discovery adds amplifiers and every six hours, it costs nobody anything,
    and `ensure_rx` remembers each slot it confirmed on the pooled session so
    the page's own check is free. It re-reads every slot each time it runs
    (one REST read per amplifier) and writes only what is wrong.

    The same rules as a page, and fewer rights:

      * a slot carrying **somebody else's** session is left alone (`SlotTaken`)
        and counted - never taken over;
      * nothing is routed, no volume is written, no zone is turned on. Only
        receive slots are touched;
      * it waits while a page is in progress (`busy()` - the API's own page
        lock - or `paging()` in this process) and only then carries on, so a
        page never shares a socket with it;
      * one line per amplifier says what it found.

    Amplifiers in parallel, zones within one in order. Returns
    {host: summary}.
    """
    busy = busy or (lambda: False)

    def idle():
        waited = False
        while busy() or paging():
            waited = True
            time.sleep(wait)
        return waited

    out = {}

    def one(host, cfg):
        t0 = time.time()
        got = {"checked": 0, "pointed": [], "ours": 0, "theirs": [], "failed": []}
        try:
            idle()
            nax = pool.get(host, cfg)
            listing = nax.zones()
            # Bus partners follow their primary; only primaries are paged.
            zones = [z for z, info in listing.items() if not (info or {}).get("bus_of")]
            streams = None
            for zone in zones:
                if idle():
                    streams = None      # a page ran; read the slots afresh
                    nax = pool.get(host, cfg)
                got["checked"] += 1
                try:
                    # Checked for real every time, not taken from what this
                    # session remembers: six hours is long enough for an
                    # installer to have given a slot to something else, and a
                    # remembered "ours" would have the next page route a room
                    # to a slot that no longer carries us.
                    nax._rx_ok.discard((nax.slot_of(zone), session_name))
                    if streams is None:
                        streams = nax.rx_streams(strict=True)
                    if nax.ensure_rx(zone, session_name, address, port, streams=streams):
                        got["pointed"].append(zone)
                    else:
                        got["ours"] += 1
                except SlotTaken:
                    got["theirs"].append(zone)
                except NaxError as e:
                    got["failed"].append(f"{zone} ({e})")
        except Exception as e:
            log(f"[nax] {host}: receive slots not warmed ({type(e).__name__}: {e})")
            out[host] = dict(got, error=f"{type(e).__name__}: {e}")
            return
        parts = [f"{got['checked']} checked", f"{got['ours']} already ours"]
        if got["pointed"]:
            parts.append(f"{len(got['pointed'])} pointed at {session_name!r} ({', '.join(got['pointed'])})")
        if got["theirs"]:
            parts.append(f"{len(got['theirs'])} carrying another session, left alone "
                         f"({', '.join(got['theirs'])})")
        if got["failed"]:
            parts.append(f"{len(got['failed'])} failed: {'; '.join(got['failed'])}")
        log(f"[nax] {host}: receive slots warm - {', '.join(parts)} in {time.time() - t0:.1f}s")
        out[host] = got

    threads = [threading.Thread(target=one, args=(h, c), daemon=True, name=f"nax-warm-{h}")
               for h, c in amps.items()]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    return out


def recover(amps, journal, *, source=SOURCE, log=print, pool=None):
    """Finish restores the journal says never happened - at start-up and on
    /repair. Never before the API's page lock is held by the caller.

    It only undoes what it can prove is still ours:

      * a route goes back to the recorded source only if the zone is STILL on
        our input - and never to our input itself, the same rule as a page;
        anything else means somebody has since chosen something, and it stands;
      * a volume goes back only if the zone is still at the level we wrote. A
        room somebody has turned since is theirs; this never moves it - so it
        cannot turn a room down that a person turned up.

    An amplifier that does not answer keeps its entries for next time - and
    "answer" includes the REST read: the zones are read strictly, because a
    failed read that came back empty made every stuck zone look "moved on",
    and the journal was wiped with nothing put back. A zone the read does not
    mention, or whose volume it does not give, is kept too. A volume is
    crossed off only when the amplifier reads it back.
    Returns {"host:zone": what was done}.
    """
    out = {}
    entries = journal.entries()
    by_host = {}
    for key, entry in entries.items():
        host, _, zone = key.rpartition(":")
        by_host.setdefault(host, {})[zone] = entry or {}
    for host, zones in by_host.items():
        cfg = amps.get(host)
        if cfg is None:
            log(f"[nax] {host}: journal has {len(zones)} zone(s) but it is not an amplifier "
                f"here any more - forgetting them")
            for zone in zones:
                journal.done(host, zone, "source", "volume")
            continue
        own = pool is None
        nax = None
        try:
            if own:
                nax = Nax(host, cfg.get("user") or "admin", cfg.get("password") or "", log=log)
                nax.login()
                nax.open()
            else:
                nax = pool.get(host, cfg)
            listing = nax.zones(strict=True)
            nax.seed_routes(listing)
        except Exception as e:
            log(f"[nax] {host}: journal restore deferred - amplifier not answering ({e})")
            if not own:
                pool.drop(host, force=True)
            elif nax is not None:
                try:
                    nax.close()
                except Exception:
                    pass
            continue
        try:
            routes = {}
            vwritten = {}
            for zone, entry in zones.items():
                if zone not in listing:
                    log(f"[nax] {host} {zone}: in the journal but not in the amplifier's "
                        f"zone list - kept for next time")
                    continue
                now = listing.get(zone) or {}
                if "source" in entry:
                    if now.get("source") == source:
                        was = entry["source"] if entry["source"] != source else ""
                        routes[zone] = was
                    else:
                        out[f"{host}:{zone}"] = "route already moved on - left"
                        journal.done(host, zone, "source")
                if "volume" in entry:
                    cur = now.get("volume")
                    if cur is None:
                        log(f"[nax] {host} {zone}: volume not readable - journal entry kept")
                    elif entry.get("set") is not None and cur == entry["set"]:
                        nax.set_volume(zone, entry["volume"])
                        log(f"[nax] {host} {zone}: volume {cur} -> {entry['volume']} "
                            f"(left raised by an earlier page)")
                        vwritten[zone] = entry["volume"]
                    else:
                        log(f"[nax] {host} {zone}: journal says volume {entry.get('volume')}, but "
                            f"it is at {cur}, not the {entry.get('set')} we set - leaving it")
                        journal.done(host, zone, "volume")
            if routes:
                for zone, n in nax.route_all(routes).items():
                    if n is not None:
                        out[f"{host}:{zone}"] = f"restored to {routes[zone]!r}"
                        log(f"[nax] {host} {zone}: restored to {routes[zone]!r} "
                            f"(left on {source} by an earlier page)")
                        journal.done(host, zone, "source")
                    else:
                        log(f"[nax] {host} {zone}: journal restore to {routes[zone]!r} did not take")
            if vwritten:
                # Crossed off only once the amplifier says so - see put_volumes.
                deadline = time.time() + SETTLE
                while vwritten:
                    back = nax.zones(strict=True)
                    for zone, want in list(vwritten.items()):
                        if (back.get(zone) or {}).get("volume") == want:
                            out[f"{host}:{zone}"] = f"volume back to {want}"
                            journal.done(host, zone, "volume")
                            vwritten.pop(zone)
                    if not vwritten or time.time() >= deadline:
                        break
                    time.sleep(0.25)
                for zone, want in vwritten.items():
                    log(f"[nax] {host} {zone}: volume {want} did not read back - kept for next time")
        except Exception as e:
            log(f"[nax] {host}: journal restore failed ({type(e).__name__}: {e}); kept for next time")
            if not own:
                pool.drop(host, force=True)
        finally:
            if own:
                try:
                    nax.close()
                except Exception:
                    pass
    return out


def stray(amps, *, source=SOURCE, log=print):
    """Zones sitting on the announcement input that nobody is announcing into.

    They should not exist: a page puts every zone back in a `finally`. When
    they do exist it means a restore did not take, and the room has been
    silent ever since - so this is worth being able to ask about, and worth
    being able to undo without making an announcement to find out.
    """
    found = {}
    for host, cfg in amps.items():
        nax = Nax(host, cfg.get("user") or "admin", cfg.get("password") or "", log=log)
        nax.login()
        for zone, info in nax.zones().items():
            if info.get("source") == source:
                found.setdefault(host, []).append(zone)
    return found


def unstick(amps, targets, *, log=print):
    """Clear the zones `stray` found, and confirm they went.

    Only ever writes an empty route, and only to a zone that is on our own
    input. It cannot take a room away from anything the house is playing.
    """
    out = {}
    for host, zones in targets.items():
        cfg = amps.get(host) or {}
        nax = Nax(host, cfg.get("user") or "admin", cfg.get("password") or "", log=log)
        nax.login()
        nax.open()
        try:
            # A fresh session has heard no pushes; without this a clear would
            # read as done before the amplifier had said anything. Strict: a
            # failed read seeded nothing and quietly undid exactly that.
            nax.seed_routes(nax.zones(strict=True))
            for zone, n in nax.route_all({z: "" for z in zones}).items():
                out[f"{host}:{zone}"] = n is not None
                log(f"[nax] {host} {zone}: "
                    + ("cleared" if n is not None else "WOULD NOT CLEAR"))
        finally:
            nax.close()
    return out
