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
import tempfile
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import clips
import naxctl
import sender

PORT = int(os.environ.get("API_PORT", "8099"))
TOKEN = os.environ.get("API_TOKEN", "")

SESSION = os.environ.get("SESSION", "HA Announce 1")
MCAST = os.environ.get("MCAST", "239.69.4.4")

# One announcement at a time, per house. Two pages talking over each other
# through one amplifier is worse than the second one waiting.
_speaking = threading.Lock()

MAX_AUDIO = 8 * 1024 * 1024        # about two minutes of anything sane
MAX_HOLD = 120                     # a page is not a broadcast
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


def log(msg):
    print(msg, flush=True)


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

    def _allowed(self):
        # Ingress already authenticates; the token is a second lock for the
        # case where somebody exposes the port on the LAN.
        if not TOKEN:
            return True
        got = self.headers.get("Authorization", "")
        return got == f"Bearer {TOKEN}"

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
                "amps": sorted(amps),
                # The single most useful thing to know when a page is silent:
                # the sender must be running for the route to bind at all.
                "note": "the stream carries silence until an announcement is sent",
            })

        if tail == "/zones":
            if not amps:
                return self._fail("No amplifier configured")
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
                    nax = naxctl.Nax(host, cfg["user"], cfg["password"], log=log)
                    nax.login()
                    for zone, info in nax.zones().items():
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
            return self._json({"ok": True, "zones": zones, "trouble": trouble})

        if tail == "/clips":
            return self._json({"ok": True, "clips": clips.list_clips()})

        return self._json({"error": "Not found"}, 404)

    # -- writing -----------------------------------------------------------
    def do_POST(self):
        if not self._allowed():
            return self._json({"error": "Unauthorized"}, 401)
        tail = self._tail()
        if tail == "/discover":
            return self._discover()
        if tail == "/repair":
            return self._repair()
        if tail == "/announce":
            return self._announce()
        if tail == "/clips":
            return self._save_clip()
        if tail == "/clipdelete":
            return self._delete_clip()
        if tail == "/clipfacing":
            return self._clip_facing()
        return self._json({"error": "Not found"}, 404)

    def _body(self, cap):
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = 0
        if length <= 0:
            return b""
        return self.rfile.read(min(length, cap))

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
        if not _speaking.acquire(blocking=False):
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
        for ref in (q.get("zones", [""])[0] or "").split(","):
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

        if not _speaking.acquire(blocking=False):
            # Honest rather than queued: by the time the first page finished,
            # the second would be stale and nobody would know why it was late.
            return self._fail("Another announcement is playing")
        # `_speak` owns the lock from here. It is released when the zones are
        # back, which now happens AFTER this response has gone out - so a page
        # arriving during that window is still refused rather than colliding.
        return self._speak(blob, amps, targets)

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
        tmp = tempfile.NamedTemporaryFile(suffix=".bin", delete=False)
        tmp.write(blob)
        tmp.close()
        try:
            pcm = sender.decode_to_pcm(tmp.name)
        except Exception as e:
            return self._fail(f"Could not decode that audio: {e}")
        finally:
            os.unlink(tmp.name)

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
                                     floor=ANNOUNCE_VOLUME, ready=info.update)
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
            "took": round(time.time() - started, 1),
        })


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
                clip_id = clips.speak_to_clip(name or text, text, user_facing=facing)
            else:
                blob = self._body(clips.MAX_BYTES + 1)
                clip_id = clips.save_audio(name, blob, user_facing=facing)
        except clips.ClipError as e:
            return self._fail(str(e))
        except Exception as e:
            return self._fail("%s: %s" % (type(e).__name__, e))
        log("[clips] saved %s" % clip_id)
        return self._json({"ok": True, "id": clip_id, "clips": clips.list_clips()})

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
