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


def _zone_order(name):
    """Zone10 after Zone9, not between Zone1 and Zone2."""
    tail = str(name).replace("Zone", "").strip()
    return (0, int(tail)) if tail.isdigit() else (1, str(name))


class NaxError(RuntimeError):
    pass


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

    def zones(self):
        """Every zone: what it is called, its source, whether audio is there."""
        # Two independent reads, so take them at the same time rather than one
        # after the other - this is the last REST round trip standing between a
        # page being asked for and the route being written.
        got = {}

        def fetch(key, path):
            try:
                got[key] = self.get(path)[path][key]
            except Exception:
                got[key] = {}

        threads = [threading.Thread(target=fetch, args=a) for a in
                   (("Routes", "AvMatrixRouting"), ("Zones", "ZoneOutputs"))]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
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
            }
        return out

    # -- the receive slot a zone listens on --------------------------------
    def rx_streams(self):
        try:
            return self.get("NaxAudio")["NaxAudio"]["NaxRx"]["NaxRxStreams"] or {}
        except Exception:
            return {}

    def ensure_rx(self, zone, session_name, address, port=5004):
        """Point ZoneN's receive slot at our stream, if it is ours to point.

        `Stream0N` belongs to `ZoneN` - Stream04 is Deck. A slot that is empty,
        or that already names our session, is ours to set.

        🔴 A slot carrying **somebody else's** session is left alone and the
        zone is refused. These are client houses, and a NAX receive slot can
        be a real distribution path between amplifiers; quietly stealing one
        would take a room off the air somewhere else in the building and the
        symptom would appear nowhere near this code.

        Returns True if it wrote, False if it was already right, and raises if
        the slot belongs to something else.
        """
        try:
            n = int(str(zone).replace("Zone", "").strip())
        except ValueError as e:
            raise NaxError(f"{zone} is not a zone name") from e
        slot = f"Stream{n:02d}"
        # Already confirmed on this session. The slot does not wander while we
        # hold the connection, and re-checking costs a REST round trip on every
        # single page - or a whole SETTLE, if it decides to write again.
        if (slot, session_name) in self._rx_ok:
            return False
        cur = self.rx_streams().get(slot) or {}
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
            raise NaxError(
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
        self._thread = threading.Thread(target=self._listen, daemon=True)
        self._thread.start()
        return self

    def alive(self):
        """Is this session still usable, without asking the amplifier?

        The websocket is the fragile half: `_listen` swallows exceptions so a
        dropped socket does not raise anywhere - it just stops updating
        `routes`, and every route write afterwards fails to land and burns six
        attempts before giving up. Checking is cheaper than that.
        """
        if self._stop or self.ws is None:
            return False
        if self._thread is not None and not self._thread.is_alive():
            return False
        return bool(getattr(self.ws, "connected", False))

    def _listen(self):
        self.ws.settimeout(0.6)
        while not self._stop:
            try:
                raw = self.ws.recv()
                text = (raw if isinstance(raw, str)
                        else raw.decode("utf-8", "replace")).strip()
                if not text:
                    continue
                routes = (json.loads(text).get("Device", {})
                          .get("AvMatrixRouting", {}).get("Routes", {}))
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
            except Exception:
                pass

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

    def route_all(self, wanted, attempts=ATTEMPTS, settle=SETTLE):
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
        """
        out = {}
        for zone, source in wanted.items():
            out[zone] = None
            for i in range(attempts):
                self.route(zone, source)
                # Wait for it to LAND, rather than for a fixed settle. The
                # websocket pushes the change, so this is usually about a
                # tenth of a second; it used to cost a flat 1.5 s whether the
                # device answered instantly or not.
                if not self._lands(zone, source, settle):
                    continue
                # Then watch it for a full settle, because this is exactly the
                # moment the device is about to clear it, and the only way to
                # know it did not is to keep looking. Continuously, not at the
                # two instants the old code sampled - a route that was cleared
                # and re-bound in between used to pass.
                if self._stays(zone, source, settle):
                    out[zone] = i + 1
                    break
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
        time.sleep(0.8)
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
    """

    def __init__(self, log=print):
        self._conns = {}
        self._lock = threading.Lock()
        self.log = log

    def get(self, host, cfg):
        with self._lock:
            nax = self._conns.get(host)
            if nax is not None:
                if nax.alive():
                    return nax
                self.log(f"[nax] {host}: pooled session went away, reconnecting")
                self._retire(host, nax)
            nax = Nax(host, cfg.get("user") or "admin",
                      cfg.get("password") or "", log=self.log)
            nax.login()
            nax.open()
            self._conns[host] = nax
            return nax

    def drop(self, host):
        """Throw a session away - call this whenever one has misbehaved."""
        with self._lock:
            nax = self._conns.pop(host, None)
            if nax is not None:
                self._retire(host, nax)

    def _retire(self, host, nax):
        self._conns.pop(host, None)
        try:
            nax.close()
        except Exception:
            pass

    def close(self):
        with self._lock:
            for host, nax in list(self._conns.items()):
                self._retire(host, nax)


def announce_many(amps, targets, play, *, source=SOURCE, restore=None,
                  log=print, session_name="", address="", floor=None,
                  ready=None, pool=None, port=5004, home=None):
    """Switch zones on one or more amplifiers, play once, put them all back.

    `amps` is {host: {"user":…, "password":…}}; `targets` is {host: [zones]}.

    One `play()` for all of them, because there is only one stream: AES67 is
    multicast, so every amplifier on the LAN hears the same audio at the same
    moment. Paging four rooms on two amplifiers is four routes and one voice,
    not two announcements that would arrive a second apart and echo.

    Restoring happens in a `finally`, because a zone left on a silent AES67
    input is a room whose music never comes back - and that is a worse failure
    than the announcement not playing at all. Every amplifier is restored even
    if an earlier one threw on the way in.

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
    # What Crestron Home thinks is playing, read ONCE per page. The amplifier is
    # not what pauses the music - the processor is: it sees the zone leave its
    # player and pauses it, and only the processor's own Play brings it back.
    # A zone is matched to its media room by NAME, which is the one thing the
    # amplifier and the processor were both configured with from the same list.
    playing_rooms = {}
    if home is not None:
        try:
            playing_rooms = home.rooms_playing()
        except Exception as e:
            log(f"[crpc] could not read what Crestron Home is playing ({e}); "
                f"music will not be resumed after this page")
    try:
        for host, zones in targets.items():
            cfg = amps.get(host) or {}
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
                    if pool is not None:
                        nax = pool.get(host, cfg)
                    else:
                        nax = Nax(host, cfg.get("user") or "admin",
                                  cfg.get("password") or "", log=log)
                        nax.login()
                        nax.open()
                    was_all = nax.zones()
                    break
                except Exception as e:
                    if pool is None or attempt == 2:
                        raise
                    log(f"[nax] {host}: pooled session did not answer "
                        f"({type(e).__name__}), rebuilding it")
                    pool.drop(host)
            conns[host] = nax
            taking = []
            for zone in zones:
                if session_name:
                    # A zone whose receive slot belongs to something else is
                    # refused rather than taken over - see `ensure_rx`. The
                    # page still goes ahead everywhere else, because one
                    # awkward room should not silence the whole house.
                    try:
                        if nax.ensure_rx(zone, session_name, address, port):
                            log(f"[nax] {host} {zone}: receive slot pointed at "
                                f"{session_name!r}")
                    except NaxError as e:
                        log(f"[nax] {host} {zone}: {e}")
                        refused.append(f"{host}:{zone}")
                        continue
                was = ((restore or {}).get(host) or {}).get(zone)
                if was is None:
                    was = was_all.get(zone, {}).get("source", "")
                # 🔴 Never restore a zone to our own announcement input.
                #
                # If a zone is already on `Aes67` when a page starts, it is
                # not because the house was listening to us - it is because an
                # earlier page failed to put it back. Recording that as "what
                # it was playing" makes the fault permanent: every page from
                # then on faithfully restores the room to a silent input, and
                # the log says "restored" each time. Five rooms at 14 Malke
                # were stuck this way, cemented by every announcement after
                # the first.
                if was == source:
                    log(f"[nax] {host} {zone}: was already on {source} - an "
                        f"earlier announcement did not put it back; clearing "
                        f"it instead of restoring it")
                    was = ""
                before[(host, zone)] = was
                # Only rooms whose music is PLAYING now are resumed afterwards.
                # A room that was already paused stays paused: this never
                # starts music, it only puts back what the page interrupted.
                room = (was_all.get(zone, {}).get("name") or "").strip().lower()
                if room in playing_rooms:
                    resume[(host, zone)] = playing_rooms[room]
                if floor:
                    had = (was_all.get(zone) or {}).get("volume")
                    if had is not None and had < floor:
                        volumes[(host, zone)] = had
                        nax.set_volume(zone, floor)
                        log(f"[nax] {host} {zone}: volume {had} -> {floor} "
                            f"for the announcement")
                taking.append(zone)
            # All of this amplifier's zones together - see `route_all`.
            for zone, n in nax.route_all({z: source for z in taking}).items():
                if n is None:
                    log(f"[nax] {host} {zone}: {source} would not bind")
                    refused.append(f"{host}:{zone}")
                else:
                    log(f"[nax] {host} {zone}: {source} bound after {n} "
                        f"write(s), was {before[(host, zone)]!r}")
                    taken.setdefault(host, []).append(zone)
        if len(before) > len(refused) or not refused:
            # Everything the caller needs to answer with is already known here:
            # which zones were taken, what each will go back to, which were
            # turned up. Handing it over now lets a caller reply as soon as the
            # room has heard the announcement instead of waiting out the
            # restore - which happens at the same moment either way.
            if ready:
                try:
                    ready({
                        "restored": {f"{h}:{z}": w for (h, z), w in before.items()},
                        "raised": {f"{h}:{z}": v for (h, z), v in volumes.items()},
                        "refused": list(refused),
                        # When the LAST zone bound. Its lead has been running
                        # ever since - the caller should wait out whatever is
                        # left of it, not start a fresh one.
                        "landed_at": max(
                            [t for c in conns.values() for t in c.landed.values()]
                            or [time.time()]),
                    })
                except Exception as e:
                    log(f"[nax] ready callback raised, continuing: {e}")
            # Hold the routes while the clip plays. The watch in route_all only
            # proves a route survived its first settle; with the amplifier's
            # auto-route on, a zone's own streaming player can take it back at
            # any point after that, and the listener sees it within a tenth of
            # a second. Until now nobody looked, so a page cut off halfway
            # looked exactly like one that played.
            halt = threading.Event()

            def hold():
                while not halt.wait(0.1):
                    for h, zs in taken.items():
                        nax = conns.get(h)
                        for z in zs:
                            if nax and not nax._holding(z, source):
                                key = f"{h}:{z}"
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
        # Per amplifier and all at once, for the same reason as above - and
        # this half matters more. Every second here is a second somebody's
        # music is still off.
        back = {}
        for (host, zone), was in before.items():
            back.setdefault(host, {})[zone] = was
        for host, wanted in back.items():
            nax = conns.get(host)
            if not nax:
                continue
            try:
                for zone, n in nax.route_all(wanted).items():
                    if n is None:
                        # The failure worth shouting about: a room left on a
                        # silent input is a room whose music never comes back.
                        log(f"[nax] {host} {zone}: COULD NOT RESTORE to "
                            f"{wanted[zone]!r}")
                    else:
                        log(f"[nax] {host} {zone}: restored to {wanted[zone]!r}")
            except Exception as e:
                log(f"[nax] {host}: COULD NOT RESTORE ({e})")
        # Right after the routes, so the zone is back on its own input when
        # the music starts again; before the volume, so the room does not come
        # up to level in silence and then start. This is the call the Crestron
        # Home app makes when somebody presses Play - measured on the
        # processor's own log, and proven to bring the Deck back in 1.5 s.
        for (host, zone), source_id in resume.items():
            try:
                home.play(source_id)
                log(f"[crpc] {host} {zone}: asked Crestron Home to resume source {source_id}")
            except Exception as e:
                log(f"[crpc] {host} {zone}: COULD NOT RESUME source {source_id} ({e})")
        # After the routes, so the room is back on its own source before it
        # comes back up to its own level.
        for (host, zone), had in volumes.items():
            nax = conns.get(host)
            if not nax:
                continue
            try:
                nax.set_volume(zone, had)
                log(f"[nax] {host} {zone}: volume back to {had}")
            except Exception as e:
                log(f"[nax] {host} {zone}: COULD NOT RESTORE VOLUME to "
                    f"{had} ({e})")
        # A pooled session stays open - that is the whole point of it. Only
        # connections this call built are torn down here.
        if pool is None:
            for nax in conns.values():
                try:
                    nax.close()
                except Exception:
                    pass
    return {"restored": {f"{h}:{z}": w for (h, z), w in before.items()},
            "raised": {f"{h}:{z}": v for (h, z), v in volumes.items()},
            "contested": contested,
            "resumed": {f"{h}:{z}": sid for (h, z), sid in resume.items()},
            "refused": refused}


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
            for zone, n in nax.route_all({z: "" for z in zones}).items():
                out[f"{host}:{zone}"] = n is not None
                log(f"[nax] {host} {zone}: "
                    + ("cleared" if n is not None else "WOULD NOT CLEAR"))
        finally:
            nax.close()
    return out
