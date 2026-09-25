"""The add-on's control surface: what HomeUI talks to.

Reached through Home Assistant's ingress, so the only thing that can call it
is something already holding a token for this house. That matters more than it
sounds: this endpoint can put a voice into every room, and the Pi is on the
same flat LAN as everything else.

    GET  /health          is the stream up, how many amplifiers are configured
    GET  /zones           every zone on every amplifier, named and with status
    POST /discover        {host, user, password} -> that amplifier's zones
    POST /announce        audio in the body, zones in the query -> speak
    POST /repair          put back any zone left on the announcement input
    GET  /voices          the voices the clip editor offers, and whether Fish is set up
    GET  /cliptext?id=    what a clip says (typed, or transcribed once and kept)
    POST /tts?text=&voice=&speed=      speak without saving - the editor's Play
    POST /clipregen?id=&text=&voice=&speed=   speak new words into an existing clip
    POST /fleetkey        {fish_api_key} -> checked with Fish, kept in /homeassistant/.bav_fleet.json

**A house can have more than one amplifier**, and most of the shape here comes
from that. Zones are named `<host>:ZoneN` throughout, because `Zone4` alone
stops meaning anything the moment a second DM NAX appears. One announcement
still plays once: AES67 is multicast, so every amplifier on the LAN hears the
same stream at the same instant - paging six rooms across two amplifiers is
six routes and one voice, not two announcements a second apart.

`/discover` exists so that HomeUI can fill in a zone list while somebody is
still typing an amplifier's password, before anything has been saved. It takes
credentials in the body rather than the query string, because a query string
ends up in logs.

`/announce` is deliberately synchronous. The caller wants to know whether a
page was actually made, and the whole thing takes as long as the recording
plus about four seconds of routing.

Anything the *amplifier* refuses is answered **200 with `ok: false`**, not a
5xx - see `_fail`. Only the request itself gets a real error code.
"""
import json
import os
import queue
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import clips
import crpcmedia
import discover
import naxctl
import sender
import zonelist

PORT = int(os.environ.get("API_PORT", "8099"))
# The STREAM's port, which the amplifier's receive slots must ask for. Not PORT
# above - that is the port this HTTP server listens on. 0.5.2 confused the two
# and "corrected" a working slot to 8099, which refused every page.
RTP_PORT = int(os.environ.get("PORT") or 5004)
TOKEN = os.environ.get("API_TOKEN", "")

SESSION = os.environ.get("SESSION", "HA Announce 1")
MCAST = os.environ.get("MCAST", "239.69.4.4")

# One announcement at a time, per house. Two pages talking over each other
# through one amplifier is worse than the second one waiting.
_speaking = threading.Lock()
# How long a page waits for the one before it to finish restoring, before it is
# refused. The restore runs on past the response; a tap in that window used to
# be dropped without a trace.
WAIT_FOR_TURN = 10.0

# One logged-in session per amplifier, shared by pages and by the tile's zone
# list. See naxctl.Pool: this is what removes the login and websocket handshake
# from the front of every announcement.
_pool = naxctl.Pool(log=lambda m: log(m))

MAX_AUDIO = 8 * 1024 * 1024        # about two minutes of anything sane
MAX_HOLD = 120                     # a page is not a broadcast
# A live page, in the stream's own format: 288000 bytes a second. This is the
# same ceiling as MAX_HOLD, expressed in bytes.
MAX_LIVE = MAX_HOLD * 48000 * 2 * 3
MAX_BODY = 8192                    # for the JSON endpoints, not the audio one

# Silence padded around every clip, both measured by ear at 14 Malke rather than
# derived - the buffers involved are not all ours to inspect.
#
# LEAD covers the zone FADING IN on its new source: switch and speak immediately
# and the first quarter second is inside the ramp. 0.6 was still clipping.
#
# TAIL covers the whole chain still holding audio when the last buffer is pushed -
# appsrc, the sink, the network, and the NAX's own AES67 receive buffer, which is
# the one we cannot see. Measured behaviour says that adds up to about two seconds,
# so 1.5 was under it and the last half second was being cut by the restore.
#
# Both are overridable, because the right numbers are a property of the house's
# amplifier and network rather than of this code.
def _secs(name, default):
    """An unset add-on option arrives as an empty string, not as absent."""
    try:
        return float(os.environ.get(name) or default)
    except ValueError:
        return float(default)


LEAD_SECONDS = _secs("LEAD_SECONDS", 1.5)
TAIL_SECONDS = _secs("TAIL_SECONDS", 3.0)


def _floor():
    """The volume a zone is brought up to for an announcement, 0-900.

    0 or empty leaves every zone exactly as it was found, which is the right
    setting for a house that would rather miss a page than have one raise the
    volume in a room by itself.
    """
    try:
        return int(float(os.environ.get("ANNOUNCE_VOLUME") or 600)) or None
    except ValueError:
        return 600


ANNOUNCE_VOLUME = _floor()

OPTIONS_FILE = os.environ.get("OPTIONS_FILE", "/data/options.json")
# The Supervisor keeps a saved option set, but rewrites /data/options.json only
# when the add-on STARTS. A save from the settings page therefore also lands
# here, and this is read first, so it takes effect on the next page rather than
# the next restart. Both survive a restart with the same values.
SETTINGS_FILE = os.environ.get("SETTINGS_FILE", "/data/settings.json")
SETTINGS_HTML = os.path.join(os.path.dirname(os.path.abspath(__file__)), "settings.html")


def settings():
    """Speaker groups and per-zone volumes, read fresh from the add-on's own
    options file each time - the settings page writes them through the
    Supervisor, and a page should honour a save made a second ago."""
    o = {}
    for path in (SETTINGS_FILE, OPTIONS_FILE):
        try:
            with open(path) as f:
                o = json.load(f)
            break
        except Exception:
            continue
    percent = o.get("default_percent")
    try:
        percent = int(percent) if percent not in (None, "") else 70
    except ValueError:
        percent = 70
    vols = {}
    for v in o.get("zone_volumes") or []:
        try:
            vols[str(v.get("zone"))] = max(0, min(100, int(v.get("percent"))))
        except (TypeError, ValueError):
            continue
    groups = []
    for g in o.get("speaker_groups") or []:
        zones = [str(z) for z in (g.get("zones") or []) if z]
        if g.get("name") and zones:
            groups.append({"name": str(g["name"]), "zones": zones, "page_group": str(g.get("page_group") or "")})
    return {"default_percent": percent, "zone_volumes": vols, "speaker_groups": groups,
            # What plays before/after every announcement unless the clip or
            # the request says otherwise: "" or a clip id (one of the sounds).
            "chime_before": str(o.get("chime_before") or "").strip(),
            "chime_after": str(o.get("chime_after") or "").strip()}


def floors():
    """{host:zone: level 0-900, "*": default}. Percent as Crestron Home shows
    it, times ten - measured: the Deck at 41% reads 410 on the amplifier."""
    st = settings()
    out = {"*": st["default_percent"] * 10}
    for z, pct in st["zone_volumes"].items():
        out[z] = pct * 10
    return out


def resolve_zones(spec, amps, home):
    """Turn the caller's `zones` into real zones.

    "auto"         the speaker group tied to the page group somebody is paging
                   right now (or named like it); else every zone.
    "group:NAME"   that speaker group.
    anything else  the comma-separated list it always was.
    """
    st = settings()
    spec = (spec or "").strip()
    chosen, why = None, ""
    if spec.lower() == "auto":
        names = []
        if home is not None:
            try:
                names = home.current_page_group_names()
            except Exception as e:
                log(f"[crpc] could not tell which page group is active ({e})")
        for n in names:
            g = next((g for g in st["speaker_groups"] if g["page_group"].lower() == n.lower()), None) \
                or next((g for g in st["speaker_groups"] if g["name"].lower() == n.lower()), None)
            if g:
                chosen, why = g["zones"], f"page group {n!r} -> speaker group {g['name']!r}"
                break
        if chosen is None:
            why = f"page group {names or 'unknown'}: no speaker group tied to it, using every zone"
    elif spec.lower().startswith("group:"):
        name = spec[6:].strip()
        g = next((g for g in st["speaker_groups"] if g["name"].lower() == name.lower()), None)
        if g:
            chosen, why = g["zones"], f"speaker group {g['name']!r}"
        else:
            why = f"no speaker group named {name!r}, using every zone"
    else:
        return [z.strip() for z in spec.split(",") if z.strip()], ""
    if chosen is None:
        chosen = [f"{h}:{z}" for h in amps for z in ("Zone%d" % i for i in range(1, 9))]
    return chosen, why

# The Crestron Home processor, for putting music back after a page. Optional:
# without it a page still works, but a room that was playing one of the
# amplifier's own streaming players comes back paused.
CRPC_HOST = (os.environ.get("CRPC_HOST") or "").strip()
CRPC_PIN = (os.environ.get("CRPC_PIN") or "2129918115").strip()
# Built here, AFTER its settings exist - 0.6.1 built it next to the pool,
# forty lines above them, and the add-on died on import.
_home = crpcmedia.CrestronHome(CRPC_HOST, CRPC_PIN, log=lambda m: log(m)) if CRPC_HOST else None


def log(msg):
    # Timestamped since 0.14.1: Home Assistant's add-on log shows none of its
    # own, and "the client did not hear the doorbell at 11:18" could not be
    # matched to a line. The Supervisor passes the house's TZ.
    print(time.strftime("%Y-%m-%d %H:%M:%S ") + str(msg), flush=True)


def amplifiers():
    """{host: {"user":…, "password":…}} from the add-on's options.

    `amps` is the list; `nax_host` and friends are what a single-amplifier
    house was configured with before there could be more than one, and are
    still honoured so that an upgrade does not silence a working house.
    """
    out = {}
    try:
        raw = json.loads(os.environ.get("AMPS_JSON") or "[]")
    except ValueError:
        log("[api] AMPS_JSON is not JSON - ignoring it")
        raw = []
    for a in raw if isinstance(raw, list) else []:
        host = str((a or {}).get("host") or "").strip()
        if host:
            out[host] = {"user": str(a.get("user") or "admin").strip(),
                         "password": str(a.get("password") or "")}
    legacy = os.environ.get("NAX_HOST", "").strip()
    if legacy and legacy not in out:
        out[legacy] = {"user": os.environ.get("NAX_USER", "admin"),
                       "password": os.environ.get("NAX_PASS", "")}
    # Found on the network since this process started. The options hold them
    # too, but AMPS_JSON was read at start; this is how a page reaches an
    # amplifier discovered five minutes ago without a restart.
    for host, cfg in discover.DISCOVERED.items():
        out.setdefault(host, dict(cfg))
    return out


def split_zone(ref):
    """`192.168.0.51:Zone4` -> ('192.168.0.51', 'Zone4').

    A bare `Zone4` is allowed and means the only amplifier there is, so a
    single-amplifier house never has to learn about this at all.
    """
    ref = str(ref or "").strip()
    if ":" in ref:
        host, _, zone = ref.rpartition(":")
        host, zone = host.strip(), zone.strip()
    else:
        host, zone = "", ref
    if zone and not zone.startswith("Zone"):
        zone = f"Zone{zone}"
    return host, zone


def house_zones(amps):
    """The zone list `/zones` serves - every amplifier's zones shaped around
    Crestron Home's rooms - with {host: why} for amplifiers that did not
    answer, and the room count (None without a processor)."""
    zones, trouble = {}, {}
    for host, cfg in amps.items():
        if not cfg.get("password"):
            # Worth its own sentence: without it the amplifier answers
            # 403, and `naxctl` reads a 403 as the Origin trap - which
            # is the right guess when a password *was* given and
            # exactly the wrong thing to tell somebody who has not
            # typed one yet.
            trouble[host] = "No password is saved for this amplifier"
            continue
        try:
            # Through the pool as well, so the tile's list is served by
            # the same session a page uses instead of logging in again.
            try:
                nax = _pool.get(host, cfg)
                listing = nax.zones()
            except Exception:
                _pool.drop(host)
                nax = _pool.get(host, cfg)
                listing = nax.zones()
            for zone, info in listing.items():
                zones[f"{host}:{zone}"] = dict(
                    info, host=host, zone=zone,
                    # On our own input with nothing being announced -
                    # a restore that did not take, and a silent room.
                    stray=info.get("source") == naxctl.SOURCE)
        except Exception as e:
            # One unreachable amplifier must not hide the others; a
            # house with two of them still pages through the one that
            # is answering.
            trouble[host] = f"{type(e).__name__}: {e}"
    # Shaped like the house: Crestron Home's media rooms, in its
    # order, each with its speakers; a bussed pair as one; a room
    # whose speakers are not on any amplifier here still listed.
    rooms = None
    if _home is not None:
        try:
            rooms = _home.media_rooms()
        except Exception as e:
            log(f"[api] zones: processor did not give its rooms ({e}); amplifier order")
    zones = zonelist.compose(zones, rooms)
    return zones, trouble, rooms


class Handler(BaseHTTPRequestHandler):
    server_version = "NaxAnnounce"

    def _json(self, obj, code=200):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _fail(self, msg):
        log(f"[api] refused: {msg}")
        """An amplifier-level failure, answered 200.

        🔴 Not a 5xx. This add-on is reached through Home Assistant, and a
        house behind a Cloudflare tunnel gets its origin's 502 **replaced**
        with Cloudflare's own `error code: 502` in plain text - so the one
        sentence worth reading ("Zone 4 would not bind", "403 from the
        amplifier") never arrives. Real HTTP codes are kept for real HTTP
        problems, which are the ones a proxy is entitled to have opinions
        about.
        """
        return self._json({"ok": False, "error": msg})

    def _ingress(self):
        """A request that came through Home Assistant's ingress.

        The Supervisor proxies those with an `X-Ingress-Path` header, from its
        own network (172.30.32.0/23) - which nothing on the house LAN can
        forge as a source address. Home Assistant has already made the person
        log in, so the token is not asked for again. Until 0.10.1 it was, and
        "Open Web UI" showed `{"error": "Unauthorized"}` (110 Roosevelt).
        """
        if not self.headers.get("X-Ingress-Path"):
            return False
        ip = (self.client_address or ("",))[0]
        return ip.startswith("172.30.32.") or ip.startswith("172.30.33.")

    def _allowed(self):
        # Ingress already authenticates; the token is a second lock for the
        # case where somebody exposes the port on the LAN.
        if not TOKEN or self._ingress():
            return True
        got = self.headers.get("Authorization", "")
        return got == f"Bearer {TOKEN}"

    def _wants_page(self):
        return "text/html" in (self.headers.get("Accept") or "")

    def _tail(self):
        # Ingress prefixes every path; only the tail is ours.
        path = urllib.parse.urlparse(self.path).path.rstrip("/")
        return "/" + path.rsplit("/", 1)[-1] if path else "/"

    def log_message(self, fmt, *args):
        log("[api] " + (fmt % args))

    # -- reading -----------------------------------------------------------
    def do_GET(self):
        if not self._allowed():
            return self._json({"error": "Unauthorized"}, 401)
        tail = self._tail()
        amps = amplifiers()

        # Ingress lands on "/": a browser gets the page, a program the health.
        if tail == "/" and self._wants_page():
            tail = "/ui"
        if tail in ("/health", "/"):
            return self._json({
                "ok": True,
                "streaming": True,
                "playing": sender.is_playing(),
                "session": SESSION,
                "multicast": MCAST,
                # Surfaced so the padding can be tuned by ear without guessing
                # whether the option actually reached the process.
                "lead_seconds": LEAD_SECONDS,
                "tail_seconds": TAIL_SECONDS,
                "announce_volume": ANNOUNCE_VOLUME,
                # Set when a processor is configured: music is resumed after a
                # page. Empty means rooms come back paused.
                "crestron_home": CRPC_HOST or None,
                "amps": sorted(amps),
                # The single most useful thing to know when a page is silent:
                # the sender must be running for the route to bind at all.
                "discovery": discover.public(),
                "note": "the stream carries silence until an announcement is sent",
            })

        if tail == "/ui":
            try:
                with open(SETTINGS_HTML, "rb") as f:
                    body = f.read()
            except OSError:
                return self._json({"error": "settings page missing"}, 500)
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if tail == "/config":
            st = settings()
            zones = {}
            for host, cfg in amps.items():
                try:
                    nax = _pool.get(host, cfg)
                    for zone, info in nax.zones().items():
                        zones[f"{host}:{zone}"] = {"name": info.get("name"), "volume": info.get("volume")}
                except Exception as e:
                    log(f"[api] config: {host} not answering ({e})")
            groups = []
            if _home is not None:
                try:
                    groups = _home.page_groups()
                except Exception as e:
                    log(f"[api] config: processor not answering ({e})")
            return self._json({"ok": True, "config": {
                "default_percent": st["default_percent"],
                "zone_volumes": [{"zone": z, "percent": p} for z, p in st["zone_volumes"].items()],
                "speaker_groups": st["speaker_groups"],
                "chime_before": st["chime_before"], "chime_after": st["chime_after"]},
                "zones": zones, "page_groups": groups,
                "active_page": (_home.active_page if _home is not None else None)})
        if tail == "/zones":
            if not amps:
                return self._fail("No amplifier configured")
            zones, trouble, rooms = house_zones(amps)
            return self._json({"ok": True, "zones": zones, "trouble": trouble,
                               "rooms": len(rooms) if rooms is not None else None})

        if tail == "/clips":
            return self._json({"ok": True, "clips": clips.list_clips()})
        if tail == "/clipaudio":
            # The page's Play buttons fetch this with a GET. Until 0.13.0 it
            # was routed for POST only, so every Play on the page was a 404.
            return self._clip_audio()
        if tail == "/voices":
            key, voice, _model = clips.fish_settings()
            return self._json({"ok": True, "voices": clips.VOICES, "default": voice,
                               # Never the key itself - only whether there is one.
                               "fish": bool(key),
                               # "options", "fleet" (the house's private file), "env" or None
                               "fish_source": clips.fish_key_source(),
                               "speed": {"min": clips.SPEED_MIN, "max": clips.SPEED_MAX}})
        if tail == "/cliptext":
            q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            try:
                return self._json(dict(clips.clip_text((q.get("id", [""])[0] or "").strip(), log=log), ok=True))
            except clips.ClipError as e:
                return self._fail(str(e))

        return self._json({"error": "Not found"}, 404)

    # -- writing -----------------------------------------------------------
    def do_POST(self):
        if not self._allowed():
            return self._json({"error": "Unauthorized"}, 401)
        tail = self._tail()
        if tail == "/discover":
            return self._discover()
        if tail == "/scan":
            return self._scan()
        if tail == "/repair":
            return self._repair()
        if tail == "/announce":
            return self._announce()
        if tail == "/config":
            return self._save_config()
        if tail == "/live":
            return self._live()
        if tail == "/clips":
            return self._save_clip()
        if tail == "/clipaudio":
            return self._clip_audio()
        if tail == "/clipchime":
            return self._clip_chime()
        if tail == "/clipdelete":
            return self._delete_clip()
        if tail == "/clipfacing":
            return self._clip_facing()
        if tail == "/tts":
            return self._tts_preview()
        if tail == "/clipregen":
            return self._clip_regen()
        if tail == "/fleetkey":
            return self._fleet_key()
        return self._json({"error": "Not found"}, 404)

    def _save_config(self):
        try:
            want = json.loads(self._body(MAX_BODY) or b"{}")
        except ValueError:
            return self._json({"error": "Bad request"}, 400)
        token = os.environ.get("SUPERVISOR_TOKEN") or os.environ.get("HASSIO_TOKEN")
        if not token:
            return self._fail("No Supervisor token - the add-on needs hassio_api and a with-contenv run.sh")
        try:
            with open(OPTIONS_FILE) as f:
                options = json.load(f)
        except Exception as e:
            return self._fail(f"Could not read the current options: {e}")
        if "zone_volumes" in want:
            options["zone_volumes"] = [{"zone": str(v.get("zone")), "percent": int(v.get("percent"))}
                                       for v in want["zone_volumes"] if v.get("zone") is not None]
        if "speaker_groups" in want:
            options["speaker_groups"] = [{"name": str(g.get("name")), "zones": [str(z) for z in g.get("zones") or []],
                                          "page_group": str(g.get("page_group") or "")}
                                         for g in want["speaker_groups"] if g.get("name")]
        if "default_percent" in want:
            options["default_percent"] = int(want["default_percent"])
        for key in ("chime_before", "chime_after", "fish_api_key", "fish_voice"):
            if key in want:
                options[key] = str(want[key] or "").strip()
        req = urllib.request.Request("http://supervisor/addons/self/options", method="POST",
                                     data=json.dumps({"options": options}).encode(),
                                     headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=15) as r:
                res = json.loads(r.read().decode("utf-8", "replace"))
        except urllib.error.HTTPError as e:
            return self._fail(f"Supervisor refused the options: {e.code} {e.read()[:200].decode('utf-8', 'replace')}")
        except Exception as e:
            return self._fail(f"Supervisor not reachable: {e}")
        if res.get("result") != "ok":
            return self._fail(f"Supervisor said {res}")
        try:
            tmp = SETTINGS_FILE + ".tmp"
            with open(tmp, "w") as f:
                json.dump(options, f)
            os.replace(tmp, SETTINGS_FILE)
        except OSError as e:
            log(f"[api] settings accepted by the Supervisor but not written locally ({e}); "
                f"they apply after a restart")
        log(f"[api] settings saved: {len(options.get('speaker_groups') or [])} group(s), "
            f"{len(options.get('zone_volumes') or [])} zone volume(s)")
        return self._json({"ok": True})

    def _body(self, cap):
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = 0
        if length <= 0:
            return b""
        return self.rfile.read(min(length, cap))

    def _scan(self):
        """Find the amplifiers on this network and add the ones that let us in."""
        try:
            want = json.loads(self._body(MAX_BODY) or b"{}")
        except ValueError:
            want = {}
        user = str(want.get("user") or os.environ.get("AMP_USER") or "admin").strip()
        password = str(want.get("password") or os.environ.get("AMP_PASSWORD") or "")
        subnet = str(want.get("subnet") or os.environ.get("SUBNET") or "").strip() or None
        try:
            base = sender.local_ip(MCAST)
        except Exception as e:
            return self._fail(f"could not work out this box's address: {e}")
        report = discover.run(base, user, password, amplifiers(), log=log, subnet=subnet)
        return self._json(dict(report, ok=not report.get("error")))

    def _discover(self):
        """One amplifier's zones, for a login that has not been saved yet."""
        try:
            want = json.loads(self._body(MAX_BODY) or b"{}")
        except ValueError:
            return self._json({"error": "Bad request"}, 400)
        host = str(want.get("host") or "").strip()
        if not host:
            return self._fail("Give the amplifier's address")
        user = str(want.get("user") or "admin").strip()
        password = str(want.get("password") or "")
        if not password:
            # Blank means "the one already saved for this address", so a
            # second look at a configured amplifier needs no re-typing.
            saved = amplifiers().get(host) or {}
            password = saved.get("password") or ""
            user = user or saved.get("user") or "admin"
        if not password:
            return self._fail("No password is saved for this amplifier")
        try:
            nax = naxctl.Nax(host, user, password, log=log)
            nax.login()
            found = nax.zones()
        except Exception as e:
            return self._fail(f"{type(e).__name__}: {e}")
        return self._json({"ok": True, "host": host, "zones": [
            {"id": f"{host}:{z}", "zone": z, "name": info.get("name") or "",
             "source": info.get("source", "")}
            for z, info in found.items()]})

    def _repair(self):
        """Put back every zone left sitting on the announcement input.

        Writes nothing but an empty route, and only to a zone that is on our
        own source, so it can never take a room away from something the house
        is playing. Exists because five rooms at 14 Malke were left this way
        by a restore that silently did not take, and the only way to find out
        was to look at the amplifier.
        """
        amps = amplifiers()
        if not amps:
            return self._fail("No amplifier configured")
        missing = [h for h, c in amps.items() if not c.get("password")]
        if missing:
            return self._fail(
                f"No password is saved for {', '.join(sorted(missing))}")
        if not _speaking.acquire(timeout=WAIT_FOR_TURN):
            return self._fail("An announcement is playing")
        try:
            found = naxctl.stray(amps, log=log)
            if not found:
                return self._json({"ok": True, "stray": [], "cleared": {}})
            cleared = naxctl.unstick(amps, found, log=log)
        except Exception as e:
            return self._fail(f"{type(e).__name__}: {e}")
        finally:
            _speaking.release()
        return self._json({
            "ok": True,
            "stray": [f"{h}:{z}" for h, zs in found.items() for z in zs],
            "cleared": cleared,
        })

    def _announce(self):
        amps = amplifiers()
        if not amps:
            return self._fail("No amplifier configured")

        q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
        targets, unknown = {}, []
        refs, why = resolve_zones(q.get("zones", [""])[0], amps, _home)
        if why:
            log(f"[api] zones: {why}")
        # A room listed for completeness ("no speakers found") cannot be
        # paged; asked for alongside real zones it is dropped, alone it is
        # refused in words.
        refs, roomonly = zonelist.targets_only(refs)
        if roomonly:
            # Looked up again rather than trusted: the caller's list may be
            # from a moment an amplifier was not answering (see rehome).
            try:
                live, trouble, _rooms = house_zones(amps)
            except Exception as e:
                live, trouble = {}, {"?": f"{type(e).__name__}: {e}"}
            found, still = zonelist.rehome(roomonly, live)
            if found:
                log(f"[api] zones: {roomonly} found again as {found}")
                refs = refs + [z for z in found if z not in refs]
            if still:
                why = f" ({'; '.join(f'{h}: {t}' for h, t in trouble.items())})" if trouble else ""
                log(f"[api] zones: {', '.join(still)} - no speakers on any amplifier here{why}")
                if not refs:
                    return self._fail(f"{', '.join(still)}: no speakers on any amplifier here{why}")
        for ref in refs:
            if not ref.strip():
                continue
            host, zone = split_zone(ref)
            if not host:
                # A bare zone belongs to the only amplifier there is. With two,
                # it is genuinely ambiguous and saying so beats picking one.
                if len(amps) != 1:
                    unknown.append(ref)
                    continue
                host = next(iter(amps))
            if host not in amps:
                unknown.append(ref)
                continue
            targets.setdefault(host, []).append(zone)
        if unknown:
            return self._fail(f"Not a zone here: {', '.join(unknown)}")
        if not targets:
            return self._fail("Name at least one zone")
        missing = [h for h in targets if not (amps[h].get("password"))]
        if missing:
            return self._fail(
                f"No password is saved for {', '.join(sorted(missing))}")

        # Either a stored clip by id, or audio in the body as before. The
        # driver uses the first; HomeUI's recorder still uses the second.
        clip_id = (q.get("clip", [""])[0] or "").strip()
        if clip_id:
            stored = clips.path_for(clip_id)
            if not stored:
                return self._fail("No stored clip called '%s'" % clip_id)
            with open(stored, "rb") as fh:
                blob = fh.read()
        else:
            blob = self._body(MAX_AUDIO + 1)
            if not blob:
                return self._json({"error": "No audio"}, 400)
        if len(blob) > MAX_AUDIO:
            return self._json({"error": "That is more audio than a page"}, 413)

        # A doorbell before "someone is at the front door", a chime after
        # "dinner is served": sounds are clips too, joined to the words here
        # so the words stay clean and the sound can be changed later.
        before, after = clips.resolve_chimes(clip_id or None, q.get("before", [None])[0],
                                             q.get("after", [None])[0], settings())
        parts = []
        for side, cid in (("before", before), ("after", after)):
            if not cid:
                continue
            sound = clips.audio_of(cid)
            if sound is None:
                log(f"[clips] no clip {cid!r} to play {side} - skipped")
            parts.append((side, sound))
        parts = [s for side, s in parts if side == "before" and s] + [blob] + [s for side, s in parts if side == "after" and s]
        if len(parts) > 1:
            log(f"[clips] {clip_id or 'announcement'}: " + " + ".join(
                ([before] if before else []) + ["words"] + ([after] if after else [])))

        if not _speaking.acquire(timeout=WAIT_FOR_TURN):
            # Honest rather than queued: by the time the first page finished,
            # the second would be stale and nobody would know why it was late.
            return self._fail("Another announcement is playing")
        # `_speak` owns the lock from here. It is released when the zones are
        # back, which now happens AFTER this response has gone out - so a page
        # arriving during that window is still refused rather than colliding.
        return self._speak(parts, amps, targets)

    def _speak(self, blob, amps, targets):
        # A one-element list rather than a flag: the worker thread below takes
        # the lock over when it starts, and every path that never reaches it
        # has to hand the lock back here instead.
        worker_owns = []
        try:
            return self._speak_locked(blob, amps, targets, worker_owns)
        finally:
            if not worker_owns:
                _speaking.release()

    def _speak_locked(self, blob, amps, targets, worker_owns):
        # One blob, or several to play one after another (chime, words, chime).
        pcms = []
        for part in (blob if isinstance(blob, (list, tuple)) else [blob]):
            tmp = tempfile.NamedTemporaryFile(suffix=".bin", delete=False)
            tmp.write(part)
            tmp.close()
            try:
                pcms.append(sender.decode_to_pcm(tmp.name))
            except Exception as e:
                return self._fail(f"Could not decode that audio: {e}")
            finally:
                os.unlink(tmp.name)
        pcm = clips.join_pcm(pcms) if len(pcms) > 1 else pcms[0]

        seconds = len(pcm) / (48000 * 2 * 3)
        if seconds > MAX_HOLD:
            return self._fail(f"{seconds:.0f}s is too long for a page")

        started = time.time()

        def play():
            # Routing has already happened by the time this runs; the pause is
            # the amplifier settling on the new source. Speaking into a zone
            # that has not finished switching loses the first syllable, which
            # is usually somebody's name.
            # Whatever is LEFT of the lead. The zone has been fading in since
            # its route bound, and confirming that route already took a settle,
            # so by now the lead is usually spent - waiting a fresh 1.5s here
            # was 1.5s of silence added to every announcement for nothing.
            spent = time.time() - info.get("landed_at", time.time())
            waiting = max(0.0, LEAD_SECONDS - spent)
            if waiting:
                time.sleep(waiting)
            # Say where the time actually went, every time. "How long before
            # anybody hears it" was answered for three versions by inference -
            # timing the whole call, polling a flag at 5 Hz from another
            # machine - and the answers kept being confounded by how many
            # writes the route happened to take. It is one subtraction from
            # inside the process; there is no reason to guess at it.
            log(f"[api] lead: route bound {spent:.2f}s ago, waited {waiting:.2f}s "
                f"more; audio starts {time.time() - started:.2f}s after the request")
            sender.play(pcm)

            # 🔴 Do NOT wait on is_playing() alone. It goes false when the last
            # buffer has been PUSHED, not when it has been heard - appsrc buffers
            # ahead of the sink, so the tail of the clip is still in flight at
            # that moment. And `play()` only enqueues, so the flag may not even
            # be set yet the first time this looks, a race that can end the wait
            # before a single note has left the box.
            #
            # The clip's own length is the one number here that is not a guess.
            # Wait that out, then a tail for whatever is still buffered. Getting
            # this wrong does not fail loudly: it silently clips the last word
            # off every announcement, which is how it shipped.
            deadline = time.time() + seconds + TAIL_SECONDS
            while time.time() < deadline or sender.is_playing():
                time.sleep(0.05)
            heard.set()

        # 🔴 The restore is NOT on the caller's critical path.
        #
        # Putting the zones back takes as long as taking them did, and for most
        # of this add-on's life the caller sat through it - a three second clip
        # answered in fifteen. Nothing is gained by that wait: the restore runs
        # at the same moment whether or not somebody is watching it, so all the
        # waiting bought was a later answer. What it cost was real, because the
        # driver's "Announcement Finished" event fires on this response and so
        # landed seconds after the room had gone quiet again.
        #
        # The lock is held until the restore is genuinely done, so the next page
        # is refused rather than starting on top of a half-restored house.
        info, broke, heard = {}, {}, threading.Event()

        def run():
            try:
                naxctl.announce_many(amps, targets, play, log=log,
                                     session_name=SESSION, address=MCAST,
                                     floor=floors(), ready=info.update,
                                     pool=_pool, port=RTP_PORT, home=_home)
            except Exception as e:
                broke["err"] = f"{type(e).__name__}: {e}"
                log(f"[api] announcement failed: {broke['err']}")
            finally:
                # Both of these on every path, or a failure before the audio
                # would hang this request and wedge the lock shut for good.
                heard.set()
                _speaking.release()

        worker_owns.append(True)
        threading.Thread(target=run, name="announce", daemon=True).start()
        # Generous: routing can retry, and answering late beats answering with
        # a half-truth. Reached only if the amplifier stops responding.
        heard.wait(LEAD_SECONDS + seconds + TAIL_SECONDS + 90)

        if broke:
            return self._fail(broke["err"])
        spoke = [z for z in info.get("restored", {}) if z not in info.get("refused", [])]
        if not spoke:
            return self._fail("No zone would take the announcement")
        return self._json({
            "ok": True,
            "zones": spoke,
            "refused": info.get("refused") or [],
            "seconds": round(seconds, 1),
            # What each zone is being put back to. By the time this is read the
            # restore is usually done; it is what WILL happen, not what has.
            "restored": info.get("restored") or {},
            # Which rooms were turned up to be heard, and from what. A page
            # that is inaudible in one room looks identical to a working one
            # without this.
            "raised": info.get("raised") or {},
            # Rooms whose music Crestron Home was asked to put back after the
            # restore, and the source id it was asked to play.
            "resumed": info.get("resumed") or {},
            "took": round(time.time() - started, 1),
        })


    # -- a live page, streamed in while it is still being spoken -----------
    def _live(self):
        """Play a page into the zones as it arrives.

        The body is a stream of S24BE/48k/2ch PCM - already the stream's own
        format, because whoever produced it (RavaBridge, from the page's G.711)
        can convert far more cheaply than a second pipeline here could. The
        zones are routed when the request arrives and put back when the body
        ends, so the caller closing the stream is what restores the house.

        The page trails the panels by however long routing takes; nothing is
        dropped for that, because a page that starts two seconds late is worth
        more than one missing its first two seconds.
        """
        amps = amplifiers()
        if not amps:
            return self._fail("No amplifier configured")
        q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
        targets, unknown = {}, []
        refs, why = resolve_zones(q.get("zones", [""])[0], amps, _home)
        if why:
            log(f"[api] zones: {why}")
        for ref in refs:
            if not ref.strip():
                continue
            host, zone = split_zone(ref)
            if not host:
                if len(amps) != 1:
                    unknown.append(ref)
                    continue
                host = next(iter(amps))
            if host not in amps:
                unknown.append(ref)
                continue
            targets.setdefault(host, []).append(zone)
        if unknown:
            return self._fail(f"Not a zone here: {', '.join(unknown)}")
        if not targets:
            return self._fail("Name at least one zone")
        if not _speaking.acquire(timeout=WAIT_FOR_TURN):
            return self._fail("Another announcement is playing")
        worker_owns = []
        try:
            return self._live_locked(amps, targets, worker_owns)
        finally:
            if not worker_owns:
                _speaking.release()

    def _live_locked(self, amps, targets, worker_owns):
        audio = queue.Queue(maxsize=400)
        info, broke, done = {}, {}, threading.Event()
        started = time.time()

        def play():
            while True:
                chunk = audio.get()
                if chunk is None:
                    break
                sender.play(chunk)
            # The last buffers are still in flight when the queue runs dry.
            time.sleep(TAIL_SECONDS)

        def run():
            try:
                naxctl.announce_many(amps, targets, play, log=log,
                                     session_name=SESSION, address=MCAST,
                                     floor=floors(), ready=info.update,
                                     pool=_pool, port=RTP_PORT, home=_home)
            except Exception as e:
                broke["err"] = f"{type(e).__name__}: {e}"
                log(f"[api] live page failed: {broke['err']}")
            finally:
                done.set()
                _speaking.release()

        worker_owns.append(True)
        threading.Thread(target=run, name="live-page", daemon=True).start()

        total = 0
        try:
            for chunk in self._read_stream(MAX_LIVE):
                total += len(chunk)
                audio.put(chunk, timeout=30)
        except Exception as e:
            log(f"[api] live page stream ended: {type(e).__name__}: {e}")
        finally:
            audio.put(None)

        done.wait(LEAD_SECONDS + TAIL_SECONDS + 120)
        if broke:
            return self._fail(broke["err"])
        spoke = [z for z in info.get("restored", {}) if z not in info.get("refused", [])]
        return self._json({
            "ok": bool(spoke),
            "zones": spoke,
            "refused": info.get("refused") or [],
            "seconds": round(total / (48000 * 2 * 3), 1),
            "bytes": total,
            "restored": info.get("restored") or {},
            "raised": info.get("raised") or {},
            # Rooms whose music Crestron Home was asked to put back after the
            # restore, and the source id it was asked to play.
            "resumed": info.get("resumed") or {},
            "took": round(time.time() - started, 1),
        })

    def _read_stream(self, cap):
        """The body a piece at a time, chunked or not."""
        if (self.headers.get("Transfer-Encoding") or "").lower() == "chunked":
            total = 0
            while True:
                line = self.rfile.readline(80).strip()
                if not line:
                    return
                try:
                    n = int(line.split(b";")[0], 16)
                except ValueError:
                    return
                if n == 0:
                    self.rfile.readline(8)
                    return
                data = self.rfile.read(n)
                self.rfile.read(2)
                total += len(data)
                if total > cap:
                    raise ValueError("more audio than a page")
                yield data
        else:
            blob = self._body(cap)
            if blob:
                yield blob

    # -- stored clips ------------------------------------------------------
    def _save_clip(self):
        """Keep a clip: audio in the body, or `text=` to have it spoken."""
        q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
        name = (q.get("name", [""])[0] or "").strip()
        text = (q.get("text", [""])[0] or "").strip()
        # Defaults to user-facing, because the recorder in HomeUI is somebody
        # making an announcement for their own house.
        facing = (q.get("user_facing", ["true"])[0] or "true").strip().lower() not in ("0", "false", "no")
        try:
            if text:
                clip_id = clips.speak_to_clip(name or text, text, user_facing=facing,
                                              voice=(q.get("voice", [""])[0] or "").strip() or None,
                                              speed=q.get("speed", ["1"])[0])
            else:
                blob = self._body(clips.MAX_BYTES + 1)
                clip_id = clips.save_audio(name, blob, user_facing=facing)
        except clips.ClipError as e:
            return self._fail(str(e))
        except Exception as e:
            return self._fail("%s: %s" % (type(e).__name__, e))
        log("[clips] saved %s" % clip_id)
        return self._json({"ok": True, "id": clip_id, "clips": clips.list_clips()})

    def _clip_audio(self):
        """The audio of one clip, for the page's play-back button."""
        q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
        p = clips.path_for((q.get("id", [""])[0] or "").strip())
        if not p:
            return self._json({"error": "No such clip"}, 404)
        with open(p, "rb") as fh:
            body = fh.read()
        self.send_response(200)
        self.send_header("Content-Type", clips.content_type(p))
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _tts_preview(self):
        """Speak without saving: the editor's Play. The take is remembered, so
        a Regenerate straight after saves the same reading that was heard."""
        q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
        try:
            audio, engine = clips.synthesize((q.get("text", [""])[0] or ""),
                                             (q.get("voice", [""])[0] or "").strip() or None,
                                             q.get("speed", ["1"])[0])
        except clips.ClipError as e:
            return self._fail(str(e))
        except Exception as e:
            return self._fail("%s: %s" % (type(e).__name__, e))
        head = audio[:4]
        ctype = "audio/wav" if head == b"RIFF" else "audio/mpeg"
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(audio)))
        self.send_header("X-Speech-Engine", engine)
        self.end_headers()
        self.wfile.write(audio)

    def _fleet_key(self):
        """Keep a Fish key in the house's private fleet file (never in the
        public repository), after Fish has said it works."""
        try:
            want = json.loads(self._body(MAX_BODY) or b"{}")
        except ValueError:
            return self._json({"error": "Bad request"}, 400)
        try:
            res = clips.save_fleet_key(str(want.get("fish_api_key") or ""))
        except clips.ClipError as e:
            return self._fail(str(e))
        except OSError as e:
            return self._fail(f"Could not write the fleet file: {e}")
        log(f"[clips] Fish key saved to {res['path']}")
        return self._json({"ok": True, "credit": res["credit"]})

    def _clip_regen(self):
        q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
        clip_id = (q.get("id", [""])[0] or "").strip()
        try:
            clips.regenerate_clip(clip_id, (q.get("text", [""])[0] or ""),
                                  (q.get("voice", [""])[0] or "").strip() or None,
                                  q.get("speed", ["1"])[0])
        except clips.ClipError as e:
            return self._fail(str(e))
        except Exception as e:
            return self._fail("%s: %s" % (type(e).__name__, e))
        log("[clips] regenerated %s" % clip_id)
        return self._json({"ok": True, "id": clip_id, "clips": clips.list_clips()})

    def _clip_chime(self):
        """What plays before/after one clip: `before=` / `after=` each a clip
        id, "none", or "default"."""
        q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
        clip_id = (q.get("id", [""])[0] or "").strip()
        before = q.get("before", [None])[0]
        after = q.get("after", [None])[0]
        try:
            meta = clips.set_chime(clip_id, before, after)
        except clips.ClipError as e:
            return self._fail(str(e))
        return self._json({"ok": True, "id": clip_id, "before": meta.get("before"), "after": meta.get("after"),
                           "clips": clips.list_clips()})

    def _clip_facing(self):
        """Show or hide one clip on the end user's tile."""
        q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
        clip_id = (q.get("id", [""])[0] or "").strip()
        raw = (q.get("user_facing", [""])[0] or "").strip().lower()
        facing = raw not in ("0", "false", "no")
        try:
            clips.set_user_facing(clip_id, facing)
        except clips.ClipError as e:
            return self._fail(str(e))
        log("[clips] %s user_facing=%s" % (clip_id, facing))
        return self._json({"ok": True, "clips": clips.list_clips()})

    def _delete_clip(self):
        q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
        clip_id = (q.get("id", [""])[0] or "").strip()
        try:
            clips.delete_clip(clip_id)
        except clips.ClipError as e:
            return self._fail(str(e))
        log("[clips] deleted %s" % clip_id)
        return self._json({"ok": True, "clips": clips.list_clips()})


def serve():
    srv = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    log(f"[api] listening on {PORT}"
        + (" (token required)" if TOKEN else " (no token set)"))
    srv.serve_forever()
