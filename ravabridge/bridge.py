"""Rava bridge: a door that speaks SIP rings the Crestron Home panels that speak Rava.

The panels never register anywhere. That is the whole design, and it is a
constraint rather than a preference: a Crestron Home panel put into SIP
*server* mode loses Crestron Home's own intercom - room paging and calling -
and Crestron's own CAME/AVLinkPro guide says so. So the panels stay exactly
as Crestron Home leaves them, in Rava peer-to-peer mode, and this bridge
rings them the way a 2N or a DoorBird does: an INVITE straight to each
panel's IP, G.711 audio, H.264 video.

    door station --REGISTER/INVITE--> bridge --INVITE x N--> every panel in the group
                 --RTP video-------->        --one multicast stream--> all of them, while ringing
                 <--RTP audio------->        <--RTP audio---------->  the one that answered

Measured on a TSW-770R (Crestron Home, firmware 3.003, Sept 2026):
  - a plain RTP/AVP INVITE from an arbitrary IP gets 100 then 183 with SDP:
    audio PCMU sendrecv, video H.264 payload 103 recvonly. The picture is
    wanted *while ringing*, so the door's video has to flow before anyone
    answers - early media on both sides.
  - a video m-line whose c= is a multicast address is accepted and echoed:
    the panel joins the group. So one copy of the door's H.264 feeds a whole
    house, and each panel only carries its own audio.
  - SRTP is flagged "mandatory" in the panel's settings and is not enforced.

Standard library only. `sip.py` and `rtp.py` are HomeUI's, copied in because an
add-on's build context is its own folder.
"""
import base64
import hashlib
import os
import secrets
import selectors
import socket
import threading
import time
from collections import deque

import rtp
import sip

UA = "RavaBridge/0.1"
ALLOW = "INVITE, ACK, CANCEL, BYE, OPTIONS, INFO, UPDATE, PRACK, NOTIFY, MESSAGE"
PANEL_H264_PT = 103            # what a Crestron panel calls H.264 (SIPPAYLOAD)
DEFAULT_FMTP = "profile-level-id=42801e;packetization-mode=1"
NONCE_LIFE = 600
LATE_LEG_GRACE = 32            # keep a finished leg addressable this long for straggling responses


def md5(s):
    return hashlib.md5(s.encode("utf-8")).hexdigest()


def local_address_for(host, port=5060):
    """The address of ours a packet to `host` leaves from. Connecting a UDP
    socket sends nothing; it only consults the routing table."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect((host, port or 5060))
        return s.getsockname()[0]
    except OSError:
        return "127.0.0.1"
    finally:
        s.close()


# -- small header helpers ----------------------------------------------------

def uri_of(value):
    """The URI inside a From/To/Contact, angle brackets or not, parameters off."""
    v = (value or "").strip()
    if "<" in v and ">" in v:
        return v[v.index("<") + 1:v.index(">")]
    return v.split(";")[0].strip()


def user_of(uri):
    u = uri_of(uri)
    u = u.split(":", 1)[1] if ":" in u else u
    return u.split("@")[0].split(";")[0]


def hostport_of(uri, default_port=5060):
    u = uri_of(uri)
    u = u.split(":", 1)[1] if u.lower().startswith("sip") else u
    hp = u.split("@")[-1].split(";")[0].split(">")[0]
    host, _, port = hp.partition(":")
    try:
        return host, int(port) if port else default_port
    except ValueError:
        return host, default_port


def param_of(value, name):
    """A ;parameter of a header, looked for outside the <> of the URI."""
    v = value or ""
    if ">" in v:
        v = v[v.rindex(">") + 1:]
    for piece in v.split(";")[1:] if not v.startswith(";") else v.split(";"):
        k, _, val = piece.strip().partition("=")
        if k.strip().lower() == name:
            return val.strip().strip('"')
    return None


def cseq_of(msg):
    raw = (msg.get("cseq") or "").split()
    try:
        return int(raw[0]), (raw[1].upper() if len(raw) > 1 else "")
    except (ValueError, IndexError):
        return 0, ""


# -- configuration -----------------------------------------------------------

class Panel:
    def __init__(self, cfg):
        self.name = str(cfg.get("name") or cfg.get("host"))
        self.host = str(cfg.get("host") or "").strip()
        self.port = int(cfg.get("port") or 5060)
        # The panel answers SIP addressed to any user at its IP (measured with OPTIONS), so the
        # extension is a nicety: it shows in the panel's own call log. `CRESTRON` when unknown.
        self.ext = str(cfg.get("ext") or "").strip() or "CRESTRON"
        self.hostname = str(cfg.get("hostname") or "").strip()
        self.groups = [str(g).strip().lower() for g in (cfg.get("groups") or []) if str(g).strip()]

    @property
    def ok(self):
        return bool(self.host)

    def public(self):
        return {"name": self.name, "host": self.host, "ext": self.ext, "groups": self.groups,
                "hostname": self.hostname or None}


class Door:
    """A SIP endpoint allowed to ring the house: a UniFi intercom, a 2N behind a PBX, a phone."""

    def __init__(self, cfg):
        self.name = str(cfg.get("name") or cfg.get("user") or "Door")
        self.user = str(cfg.get("user") or "").strip()
        self.password = str(cfg.get("password") or "")
        self.host = str(cfg.get("host") or "").strip()     # optional: also accept calls from this IP unregistered
        self.ring = [str(r).strip().lower() for r in (cfg.get("ring") or ["all"]) if str(r).strip()]
        self.door_id = str(cfg.get("door_id") or "").strip()      # UniFi Access door, for /unlock
        self.audio_only = False

    def public(self):
        return {"name": self.name, "user": self.user, "host": self.host or None, "ring": self.ring,
                "doorId": self.door_id or None}


# -- registrar ---------------------------------------------------------------

class Registrar:
    """Just enough of a SIP registrar for a door station to believe in.

    Digest MD5 with qop=auth, because that is what every intercom sends. A
    binding is a contact and an expiry; the door re-registers well before it.
    """

    def __init__(self, realm, users, store=None):
        self.realm = realm
        self.users = users                  # user -> password
        self.bindings = {}                  # user -> {"contact", "addr", "expires", "ua"}
        self.nonces = {}                    # nonce -> issued
        # Bindings outlive a restart. A reader registers for an hour and does
        # not know the bridge went away for ten seconds when its options were
        # saved; without this, its next doorbell press would be refused as an
        # unknown caller until the hour was up.
        self.store = store
        if store:
            try:
                import json
                with open(store, encoding="utf-8") as f:
                    saved = json.load(f)
                now = time.time()
                for u, b in saved.items():
                    if u in users and b.get("expires", 0) > now:
                        b["addr"] = tuple(b["addr"])
                        self.bindings[u] = b
            except (OSError, ValueError):
                pass

    def _save(self):
        if not self.store:
            return
        try:
            import json
            with open(self.store, "w", encoding="utf-8") as f:
                json.dump(self.bindings, f)
        except OSError:
            pass

    def challenge(self):
        now = time.time()
        for n in [n for n, t in self.nonces.items() if now - t > NONCE_LIFE]:
            self.nonces.pop(n, None)
        nonce = secrets.token_hex(16)
        self.nonces[nonce] = now
        return f'Digest realm="{self.realm}", nonce="{nonce}", algorithm=MD5, qop="auth"'

    def verify(self, header, method):
        p = sip.auth_params(header)
        user = p.get("username")
        pw = self.users.get(user)
        nonce = p.get("nonce")
        if pw is None or nonce not in self.nonces:
            return None
        ha1 = md5(f"{user}:{p.get('realm', '')}:{pw}")
        ha2 = md5(f"{method}:{p.get('uri', '')}")
        if (p.get("qop") or "").lower() == "auth":
            expect = md5(f"{ha1}:{nonce}:{p.get('nc', '')}:{p.get('cnonce', '')}:auth:{ha2}")
        else:
            expect = md5(f"{ha1}:{nonce}:{ha2}")
        return user if secrets.compare_digest(expect, p.get("response") or "") else None

    def bind(self, user, contact, addr, expires, ua=""):
        if expires <= 0:
            self.bindings.pop(user, None)
        else:
            self.bindings[user] = {"contact": contact, "addr": addr, "expires": time.time() + expires, "ua": ua}
        self._save()

    def reap(self):
        now = time.time()
        gone = [u for u, b in self.bindings.items() if b["expires"] < now]
        for u in gone:
            self.bindings.pop(u, None)
        if gone:
            self._save()

    def user_at(self, host):
        for u, b in self.bindings.items():
            if b["addr"][0] == host:
                return u
        return None

    def public(self):
        now = time.time()
        return {u: {"contact": b["contact"], "from": f"{b['addr'][0]}:{b['addr'][1]}",
                    "expiresIn": int(b["expires"] - now), "ua": b["ua"]}
                for u, b in self.bindings.items()}


# -- media relay -------------------------------------------------------------

class Relay:
    """Every media socket in one select loop. A packet in, a packet out; no
    decoding, no mixing - moving bytes is the only thing a stdlib Python
    process can promise to do for a whole house at once."""

    def __init__(self, log):
        self.sel = selectors.DefaultSelector()
        self.log = log
        self.stats = {"packets": 0, "errors": 0}
        # Registration changes are queued and applied by the relay thread
        # itself, between selects. The first version shared a lock between
        # `select()` and `add`/`remove`, and the relay thread re-took it the
        # instant it released it, so the SIP thread waited seconds to close a
        # socket - and a call's ACK was handled three seconds late.
        self.pending = deque()
        self.wake = threading.Event()

    def add(self, sock, handler):
        self.pending.append(("add", sock, handler))
        self.wake.set()

    def remove(self, sock):
        """Unregister and close, on the relay thread. Closing a socket that
        `select()` is watching is an error on Windows, so the caller hands the
        socket over rather than closing it."""
        self.pending.append(("remove", sock, None))
        self.wake.set()

    def _apply(self):
        while self.pending:
            op, sock, handler = self.pending.popleft()
            try:
                if op == "add":
                    self.sel.register(sock, selectors.EVENT_READ, handler)
                else:
                    try:
                        self.sel.unregister(sock)
                    except (KeyError, ValueError):
                        pass
                    try:
                        sock.close()
                    except OSError:
                        pass
            except Exception as e:
                self.log(f"relay {op}: {e!r}")

    def start(self):
        threading.Thread(target=self._loop, daemon=True, name="relay").start()

    def _loop(self):
        while True:
            self._apply()
            if not self.sel.get_map():
                # select() with nothing to watch raises on Windows; wait for work instead.
                self.wake.wait(0.2)
                self.wake.clear()
                continue
            try:
                ready = self.sel.select(0.1)
            except Exception:
                time.sleep(0.02)
                continue
            for key, _ in ready:
                try:
                    data, addr = key.fileobj.recvfrom(65535)
                except OSError:
                    continue
                try:
                    key.data(data, addr)
                    self.stats["packets"] += 1
                except Exception as e:
                    self.stats["errors"] += 1
                    if self.stats["errors"] % 100 == 1:
                        self.log(f"relay: {e!r}")


class VideoOut:
    """The door's H.264 packets, re-sent to the panels' multicast group.

    Two rewrites and one insertion, each because a panel needs it:
      - payload type becomes 103, the number the panel calls H.264;
      - sequence numbers become ours, so that the insertion below does not
        leave a gap the decoder reads as loss;
      - SPS/PPS from the door's `sprop-parameter-sets` are sent in front of
        the first keyframe if the door never sends them in-band. A decoder
        that has no parameter sets shows nothing, silently, forever.
    """

    def __init__(self, sock, group, sprop):
        self.sock = sock
        self.group = group
        self.seq = secrets.randbits(16)
        self.sprop = []
        for b64 in (sprop or "").split(","):
            b64 = b64.strip()
            if b64:
                try:
                    self.sprop.append(base64.b64decode(b64 + "=" * (-len(b64) % 4)))
                except Exception:
                    pass
        self.saw_sps_inband = False
        self.sent_sprop = False
        self.packets = 0

    @staticmethod
    def _nal_type(payload):
        if not payload:
            return None
        t = payload[0] & 0x1F
        if t == 28 and len(payload) > 1:               # FU-A: the real type is in the second byte
            return payload[1] & 0x1F, bool(payload[1] & 0x80)
        if t == 24 and len(payload) > 3:               # STAP-A: look at the first aggregated unit
            return payload[3] & 0x1F, True
        return t, True

    def _emit(self, marker, ts, ssrc, payload):
        self.sock.sendto(rtp.pack(pt=PANEL_H264_PT, seq=self.seq, ts=ts, ssrc=ssrc,
                                  payload=payload, marker=marker), self.group)
        self.seq = (self.seq + 1) & 0xFFFF
        self.packets += 1

    def push(self, data):
        pkt = rtp.unpack(data)
        if pkt is None:
            return
        info = self._nal_type(pkt["payload"])
        if info:
            nal, start = info
            if nal in (7, 8):
                self.saw_sps_inband = True
            elif nal == 5 and start and self.sprop and not self.saw_sps_inband and not self.sent_sprop:
                for unit in self.sprop:
                    self._emit(False, pkt["ts"], pkt["ssrc"], unit)
                self.sent_sprop = True
        self._emit(pkt["marker"], pkt["ts"], pkt["ssrc"], pkt["payload"])


# -- calls -------------------------------------------------------------------

class PanelLeg:
    """One INVITE we sent to one panel."""

    def __init__(self, call, panel):
        self.call = call
        self.panel = panel
        self.call_id = sip.new_call_id("ravabridge")
        self.from_tag = sip.new_tag()
        self.branch = sip.new_branch()
        self.cseq = 1
        self.uri = f"sip:{panel.ext}@{panel.host}:{panel.port}"
        self.to_tag = None
        self.remote_uri = self.uri            # the panel's Contact once it answers
        self.codec = None                     # which codec it chose, once it answers
        self.remote_audio = None              # (host, port) from its SDP
        self.state = "new"                    # new | ringing | early | answered | cancelled | failed | done
        self.sock = None
        self.audio_port = None
        self.finished_at = None
        self.invite_headers = None

    def open_audio(self, relay, handler):
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.bind(("0.0.0.0", 0))
        self.audio_port = self.sock.getsockname()[1]
        relay.add(self.sock, handler)

    def close(self, relay):
        if self.sock:
            relay.remove(self.sock)          # the relay closes it, on its own thread
            self.sock = None
        if self.finished_at is None:
            self.finished_at = time.time()

    def public(self):
        return {"panel": self.panel.name, "state": self.state}


class DoorCall:
    """One inbound call from a door, from the INVITE to the BYE."""

    def __init__(self, bridge, invite, addr, door, video_allowed=True):
        self.bridge = bridge
        self.invite = invite
        self.addr = addr
        self.door = door
        self.id = invite.call_id
        self.local_tag = sip.new_tag()
        self.remote_uri = uri_of(invite.get("contact")) or f"sip:{addr[0]}:{addr[1]}"
        self.display = door.name
        self.offer = sip.sdp_parse(invite.body)
        self.audio_codec = sip.pick_codec(self.offer)
        self.video = sip.pick_video(self.offer) if video_allowed else None
        audio = self.offer.get("audio") or {}
        self.door_audio = (audio.get("address"), audio.get("port")) if audio.get("port") else None
        # Everything the door said it can send, in its order. This is what the
        # panels get to choose from: the bridge copies audio packets rather than
        # transcoding them, so the menu can only ever be what the door supplies.
        self.offer_payloads = [p for p in (audio.get("payloads") or [])
                               if p in (sip.G722, rtp.PCMU, rtp.PCMA)]
        self.state = "new"                    # new | ringing | answering | up | done
        self.started = time.time()
        self.answered_at = None
        self.legs = []
        self.answered = None
        self.audio_sock = None
        self.video_sock = None
        self.mcast_sock = None
        self.video_out = None
        self.audio_port = None
        self.video_port = None
        self.our_cseq = 1
        self.why = None
        self.audio_in = 0
        self.video_in = 0

    # -- media --
    def open_media(self):
        b = self.bridge
        self.audio_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.audio_sock.bind(("0.0.0.0", 0))
        self.audio_port = self.audio_sock.getsockname()[1]
        b.relay.add(self.audio_sock, self._door_audio)
        if self.video:
            self.video_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            self.video_sock.bind(("0.0.0.0", 0))
            self.video_port = self.video_sock.getsockname()[1]
            self.mcast_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            self.mcast_sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, b.mcast_ttl)
            try:
                self.mcast_sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_IF, socket.inet_aton(b.address))
            except OSError as e:
                b.log(f"multicast interface {b.address}: {e}")
            sprop = None
            for piece in (self.video.get("fmtp") or "").split(";"):
                k, _, v = piece.strip().partition("=")
                if k.strip().lower() == "sprop-parameter-sets":
                    sprop = v.strip()
            self.video_out = VideoOut(self.mcast_sock, (b.mcast, b.mcast_port), sprop)
            b.relay.add(self.video_sock, self._door_video)

    def close_media(self):
        for s in (self.audio_sock, self.video_sock):
            if s:
                self.bridge.relay.remove(s)   # unregistered and closed on the relay thread
        if self.mcast_sock:
            try:
                self.mcast_sock.close()
            except OSError:
                pass
        self.audio_sock = self.video_sock = self.mcast_sock = None

    def _door_audio(self, data, addr):
        self.audio_in += 1
        # Symmetric RTP: the door may send from a different port than its SDP said.
        if self.door_audio != addr and rtp.unpack(data):
            self.door_audio = addr
        leg = self.answered
        if leg and leg.remote_audio and leg.sock:
            leg.sock.sendto(data, leg.remote_audio)

    def _door_video(self, data, addr):
        self.video_in += 1
        if self.video_out:
            self.video_out.push(data)

    def leg_audio(self, leg):
        def handler(data, addr):
            if leg.remote_audio != addr and rtp.unpack(data):
                leg.remote_audio = addr
            if self.answered is leg and self.door_audio and self.audio_sock:
                self.audio_sock.sendto(data, self.door_audio)
        return handler

    # -- SDP --
    def answer_sdp(self, direction):
        return sip.sdp_answer(address=self.bridge.address, audio_port=self.audio_port or 0,
                              audio_codec=self.audio_codec if self.audio_codec is not None else rtp.PCMU,
                              video_port=self.video_port, video=self.video, audio_direction=direction)

    def panel_offer(self, leg):
        b = self.bridge
        codec = self.audio_codec if self.audio_codec is not None else rtp.PCMU
        menu = self.offer_payloads or [codec]
        sid = int(time.time())
        lines = [
            "v=0", f"o=ravabridge {sid} {sid} IN IP4 {b.address}", "s=RavaBridge",
            f"c=IN IP4 {b.address}", "t=0 0",
            # Everything the door can send, in the door's order - G.722 first
            # when it has it, since that is 16 kHz and a doorbell is worth
            # hearing properly. A panel picks what it supports: a TSW-770R
            # takes the G.722, an older TSW-560 takes the G.711, and both ring.
            f"m=audio {leg.audio_port} RTP/AVP " + " ".join(str(c) for c in menu) + " 101",
            *[f"a=rtpmap:{c} {sip.rtpmap_for(c)}" for c in menu],
            "a=rtpmap:101 telephone-event/8000", "a=fmtp:101 0-15", "a=ptime:20", "a=sendrecv",
        ]
        if self.video:
            lines += [
                f"m=video {b.mcast_port} RTP/AVP {PANEL_H264_PT}",
                f"c=IN IP4 {b.mcast}/{b.mcast_ttl}",
                f"a=rtpmap:{PANEL_H264_PT} H264/90000",
                f"a=fmtp:{PANEL_H264_PT} {self.video_fmtp()}",
                "a=sendrecv",         # what was measured to work; the panel answers recvonly
            ]
        return ("\r\n".join(lines) + "\r\n").encode("ascii")

    def video_fmtp(self):
        """The door's own profile and parameter sets, passed through to the panels;
        Baseline 3.0 if the door said nothing. The G3 Reader Pro sends 42801e."""
        src = (self.video or {}).get("fmtp") or ""
        keep = [p.strip() for p in src.split(";")
                if p.strip().split("=")[0].strip().lower()
                in ("profile-level-id", "packetization-mode", "sprop-parameter-sets")]
        return ";".join(keep) if keep else DEFAULT_FMTP

    def public(self):
        return {"id": self.id, "door": self.door.name, "from": f"{self.addr[0]}:{self.addr[1]}",
                "state": self.state, "age": round(time.time() - self.started, 1),
                "video": bool(self.video), "answeredBy": self.answered.panel.name if self.answered else None,
                "legs": [l.public() for l in self.legs],
                "audioIn": self.audio_in, "videoIn": self.video_in,
                "videoOut": self.video_out.packets if self.video_out else 0, "why": self.why}


# -- the bridge --------------------------------------------------------------

class Bridge:
    def __init__(self, cfg, log=print):
        self.cfg = cfg
        self.log = log
        self.port = int(cfg.get("sip_port") or 5060)
        self.panels = [p for p in (Panel(x) for x in cfg.get("panels") or []) if p.ok]
        self.doors = {d.user: d for d in (Door(x) for x in cfg.get("doors") or []) if d.user}
        # Either shape: {"0005": ["1st floor"]} or the add-on schema's [{"number": "0005", "ring": [...]}].
        raw = cfg.get("recipients") or {}
        if isinstance(raw, dict):
            self.recipients = {str(k): v for k, v in raw.items()}
        else:
            self.recipients = {str(r.get("number")): r.get("ring") or [] for r in raw if isinstance(r, dict) and r.get("number")}
        self.mcast = str(cfg.get("mcast") or "227.1.1.2")
        self.mcast_port = int(cfg.get("mcast_port") or 40002)
        self.mcast_ttl = int(cfg.get("mcast_ttl") or 16)
        self.ring_seconds = int(cfg.get("ring_seconds") or 60)
        self.log_sip = bool(cfg.get("log_sip"))
        self.realm = str(cfg.get("realm") or "ravabridge")
        self.registrar = Registrar(self.realm, {u: d.password for u, d in self.doors.items()},
                                   store=cfg.get("state_file") or ("/data/bindings.json" if os.path.isdir("/data") else None))
        probe = self.panels[0].host if self.panels else "192.168.0.1"
        self.address = str(cfg.get("bind_ip") or "").strip() or local_address_for(probe)
        self.sock = None
        self.calls = {}                  # door call-id -> DoorCall
        self.legs = {}                   # leg call-id -> (DoorCall, PanelLeg)
        self.lock = threading.RLock()
        self.relay = Relay(log)
        self.events = deque(maxlen=60)
        self.started = time.time()
        talk = cfg.get("talk") or {}
        self.talk = TalkClient(self, talk) if talk.get("host") and talk.get("user") else None

    # -- lifecycle --
    def apply(self, options):
        """New panels and doors from discovery (or a saved options page), without a restart.
        Registrations already held are kept; a door that vanished from the options stays
        callable until its registration expires."""
        with self.lock:
            self.panels = [p for p in (Panel(x) for x in options.get("panels") or []) if p.ok]
            self.doors = {d.user: d for d in (Door(x) for x in options.get("doors") or []) if d.user}
            self.registrar.users = {u: d.password for u, d in self.doors.items()}
            raw = options.get("recipients") or {}
            if isinstance(raw, dict):
                self.recipients = {str(k): v for k, v in raw.items()}
            else:
                self.recipients = {str(r.get("number")): r.get("ring") or [] for r in raw
                                   if isinstance(r, dict) and r.get("number")}
            self.cfg = options
        self.note(f"configuration applied: {len(self.panels)} panel(s), {len(self.doors)} door(s)")

    def start(self):
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        s.bind(("0.0.0.0", self.port))
        s.settimeout(0.5)
        self.sock = s
        self.relay.start()
        threading.Thread(target=self._loop, daemon=True, name="sip").start()
        if self.talk:
            self.talk.start()
        self.note(f"listening on {self.address}:{self.port}; {len(self.panels)} panel(s), "
                  f"{len(self.doors)} door(s), video multicast {self.mcast}:{self.mcast_port} ttl {self.mcast_ttl}")

    def note(self, text):
        self.events.append({"t": time.time(), "text": text})
        self.log(text)

    def _loop(self):
        last_tick = 0
        while True:
            try:
                data, addr = self.sock.recvfrom(65535)
            except socket.timeout:
                data = None
            except OSError:
                time.sleep(0.05)
                data = None
            if data:
                msg = sip.parse(data)
                if msg is not None:
                    if self.log_sip:
                        self.log(f"<<< {addr[0]}:{addr[1]}\n{data.decode('utf-8', 'replace').rstrip()}")
                    try:
                        with self.lock:
                            self._handle(msg, addr)
                    except Exception as e:
                        self.log(f"error handling {msg!r} from {addr[0]}: {e!r}")
            if time.time() - last_tick > 1.0:
                last_tick = time.time()
                try:
                    with self.lock:
                        self._tick()
                except Exception as e:
                    self.log(f"tick: {e!r}")

    def _tick(self):
        self.registrar.reap()
        now = time.time()
        for call in list(self.calls.values()):
            if call.state == "ringing" and now - call.started > self.ring_seconds:
                self.note(f"{call.door.name}: nobody answered in {self.ring_seconds}s")
                self._respond(call.invite, 480, call.addr, to_tag=call.local_tag)
                self._finish(call, "ring timeout")
            elif call.state == "done" and now - (call.answered_at or call.started) > LATE_LEG_GRACE:
                self.calls.pop(call.id, None)
        for cid, (call, leg) in list(self.legs.items()):
            if leg.finished_at and now - leg.finished_at > LATE_LEG_GRACE:
                self.legs.pop(cid, None)

    # -- transport --
    def _send(self, data, addr):
        if self.log_sip:
            self.log(f">>> {addr[0]}:{addr[1]}\n{data.decode('utf-8', 'replace').rstrip()}")
        try:
            self.sock.sendto(data, addr)
        except OSError as e:
            self.log(f"send to {addr}: {e}")

    def _contact(self):
        return f"<sip:bridge@{self.address}:{self.port}>"

    def _respond(self, req, status, addr, *, extra=None, body=b"", to_tag=None):
        to = req.get("to") or ""
        if to_tag and "tag=" not in to:
            to = f"{to};tag={to_tag}"
        headers = {"via": req.all("via"), "from": req.get("from"), "to": to,
                   "call-id": req.call_id, "cseq": req.get("cseq"), "user-agent": UA}
        if body:
            headers["content-type"] = "application/sdp"
        headers.update(extra or {})
        self._send(sip.build(sip.Message(status=status, headers=headers, body=body)), addr)

    def _request(self, method, uri, headers, body=b"", addr=None):
        headers = dict(headers)
        headers.setdefault("max-forwards", "70")
        headers.setdefault("user-agent", UA)
        if body:
            headers.setdefault("content-type", "application/sdp")
        self._send(sip.build(sip.Message(method=method, uri=uri, headers=headers, body=body)),
                   addr or hostport_of(uri))

    # -- requests in --
    def _handle(self, msg, addr):
        if not msg.is_request:
            self._on_response(msg, addr)
            return
        m = msg.method
        if m == "REGISTER":
            self._on_register(msg, addr)
            return
        if m == "OPTIONS":
            self._respond(msg, 200, addr, extra={"allow": ALLOW, "accept": "application/sdp"})
            return
        if m == "INVITE":
            self._on_invite(msg, addr)
            return
        call = self.calls.get(msg.call_id)
        leg = self.legs.get(msg.call_id)
        if m == "ACK":
            if call and call.state == "answering":
                call.state = "up"
                self.note(f"{call.door.name}: talking to {call.answered.panel.name}")
            return
        if m == "CANCEL":
            self._respond(msg, 200, addr)
            if call and call.state == "ringing":
                self._respond(call.invite, 487, call.addr, to_tag=call.local_tag)
                self._finish(call, "the door gave up")
            return
        if m == "BYE":
            self._respond(msg, 200, addr, to_tag=(call.local_tag if call else None))
            if call:
                self._finish(call, "the door hung up")
            elif leg:
                self._panel_hung_up(*leg)
            return
        if m == "INFO":
            self._respond(msg, 200, addr)
            if leg:
                self._relay_info(leg[0], msg)
            return
        if m in ("UPDATE", "PRACK", "NOTIFY", "MESSAGE"):
            self._respond(msg, 200, addr, to_tag=(call.local_tag if call else None))
            return
        if m == "SUBSCRIBE":
            self._respond(msg, 489, addr)
            return
        self._respond(msg, 405, addr, extra={"allow": ALLOW})

    def _on_register(self, msg, addr):
        auth = msg.get("authorization")
        user = self.registrar.verify(auth, "REGISTER") if auth else None
        if not user:
            if auth:
                self.log(f"register from {addr[0]}: bad credentials for {sip.auth_params(auth).get('username')!r}")
            self._respond(msg, 401, addr, extra={"www-authenticate": self.registrar.challenge()})
            return
        contact = msg.get("contact") or ""
        expires_s = param_of(contact, "expires") or msg.get("expires") or "3600"
        try:
            expires = int(expires_s)
        except ValueError:
            expires = 3600
        if contact.strip() == "*":
            expires = 0
        self.registrar.bind(user, contact, addr, expires, msg.get("user-agent") or "")
        extra = {"expires": str(expires), "date": time.strftime("%a, %d %b %Y %H:%M:%S GMT", time.gmtime())}
        if expires and contact:
            extra["contact"] = contact if "expires=" in contact else f"{contact};expires={expires}"
        self._respond(msg, 200, addr, extra=extra)
        self.note(f"{self.doors[user].name} ({user}) registered from {addr[0]} for {expires}s"
                  if expires else f"{user} unregistered")

    def _door_for(self, msg, addr):
        user = user_of(msg.get("from"))
        door = self.doors.get(user)
        if door:
            bound = self.registrar.bindings.get(user, {}).get("addr", (None,))[0]
            if bound == addr[0] or door.host == addr[0]:
                return door
        bound_user = self.registrar.user_at(addr[0])
        if bound_user:
            return self.doors[bound_user]
        for d in self.doors.values():
            if d.host and d.host == addr[0]:
                return d
        if self.talk and self.talk.is_peer(addr[0]):
            return self.talk.door
        return None

    def _targets(self, msg, door):
        dialed = user_of(msg.uri)
        wanted = self.recipients.get(dialed) or door.ring or ["all"]
        if isinstance(wanted, str):
            wanted = [wanted]
        wanted = [str(w).strip().lower() for w in wanted if str(w).strip()]
        out = []
        for p in self.panels:
            if "all" in wanted or p.name.lower() in wanted or any(g in wanted for g in p.groups):
                if p not in out:
                    out.append(p)
        return out, dialed

    def _on_leg_reinvite(self, call, leg, msg, addr):
        """The panel re-offers the moment it answers - PJSIP refreshes the session
        once a call is up - and the offer carries where its audio now lives. The
        first real doorbell press taught this: the re-INVITE was taken for a new
        call from an unknown door, refused with 403, and the panel hung up two
        seconds after answering."""
        offer = sip.sdp_parse(msg.body)
        a = offer.get("audio") or {}
        if a.get("port"):
            leg.remote_audio = (a.get("address") or leg.panel.host, a["port"])
        payloads = a.get("payloads") or []
        codec = call.audio_codec if call.audio_codec in payloads else (sip.pick_codec(offer) if payloads else None)
        if codec is None:
            codec = rtp.PCMU
        sid = int(time.time())
        lines = ["v=0", f"o=ravabridge {sid} {sid + 1} IN IP4 {self.address}", "s=RavaBridge",
                 f"c=IN IP4 {self.address}", "t=0 0",
                 f"m=audio {leg.audio_port} RTP/AVP {codec}",
                 f"a=rtpmap:{codec} {sip.rtpmap_for(codec)}", "a=ptime:20", "a=sendrecv"]
        v = offer.get("video")
        if v and v.get("port"):
            pt = v["payloads"][0] if v.get("payloads") else PANEL_H264_PT
            if call.video:
                direction = "sendonly" if v.get("direction") == "recvonly" else "sendrecv"
                lines += [f"m=video {self.mcast_port} RTP/AVP {pt}", f"c=IN IP4 {self.mcast}/{self.mcast_ttl}",
                          f"a=rtpmap:{pt} H264/90000", f"a=fmtp:{pt} {call.video_fmtp()}", f"a={direction}"]
            else:
                lines += [f"m=video 0 RTP/AVP {pt}"]          # this call has no picture to give
        body = ("\r\n".join(lines) + "\r\n").encode("ascii")
        self._respond(msg, 200, addr, extra={"contact": self._contact(), "allow": ALLOW}, body=body)
        self.note(f"{call.door.name}: {leg.panel.name} refreshed its media (audio to {leg.remote_audio})")

    def _on_invite(self, msg, addr):
        pair = self.legs.get(msg.call_id)
        if pair:
            self._on_leg_reinvite(pair[0], pair[1], msg, addr)
            return
        existing = self.calls.get(msg.call_id)
        if existing:
            # A re-INVITE (hold, or the door confirming media after answer): same answer again.
            direction = "sendrecv" if existing.state in ("answering", "up") else "recvonly"
            self._respond(msg, 200, addr, to_tag=existing.local_tag, extra={"contact": self._contact()},
                          body=existing.answer_sdp(direction))
            return
        door = self._door_for(msg, addr)
        if door is None:
            # Not a registered address - but if it claims to be a door we know,
            # let it prove that with the door's password rather than turning it
            # away. This is what carries a doorbell press across a bridge
            # restart, and it is also what stops any LAN host from ringing the
            # house by putting a door's name in its From header.
            claimed = user_of(msg.get("from"))
            if claimed in self.doors:
                auth = msg.get("authorization") or msg.get("proxy-authorization")
                if auth and self.registrar.verify(auth, "INVITE") == claimed:
                    door = self.doors[claimed]
                    self.note(f"{door.name} called from {addr[0]} unregistered and authenticated")
                else:
                    self._respond(msg, 401, addr, extra={"www-authenticate": self.registrar.challenge()})
                    return
        if door is None:
            self.note(f"refused a call from {addr[0]} ({user_of(msg.get('from'))!r}): not a known door")
            self._respond(msg, 403, addr)
            return
        live = [c for c in self.calls.values() if c.state != "done"]
        if live:
            self.note(f"{door.name} called while {live[0].door.name} is {live[0].state}: busy")
            self._respond(msg, 486, addr)
            return
        self._respond(msg, 100, addr)
        call = DoorCall(self, msg, addr, door, video_allowed=not door.audio_only)
        if call.audio_codec is None:
            audio = (call.offer or {}).get("audio") or {}
            self.note(f"{door.name}: offered {audio.get('payloads') or 'no audio'}, we speak G.711 only")
            self._respond(msg, 488, addr, to_tag=call.local_tag)
            return
        targets, dialed = self._targets(msg, door)
        if not targets:
            self.note(f"{door.name} dialed {dialed!r}: no panel matches")
            self._respond(msg, 480, addr, to_tag=call.local_tag)
            return
        self.calls[call.id] = call
        call.open_media()
        self._respond(msg, 180, addr, to_tag=call.local_tag, extra={"contact": self._contact()})
        # Early media: the door starts sending its picture now, and the bridge is
        # already copying it to the multicast group the panels are about to join.
        self._respond(msg, 183, addr, to_tag=call.local_tag, extra={"contact": self._contact()},
                      body=call.answer_sdp("recvonly"))
        call.state = "ringing"
        self.note(f"{door.name} dialed {dialed!r}: ringing {', '.join(p.name for p in targets)}"
                  + (f", video {self.video_desc(call)}" if call.video else ", audio only"))
        for panel in targets:
            self._invite_panel(call, panel)

    def video_desc(self, call):
        v = call.video or {}
        return f"pt {v.get('payload')} -> {PANEL_H264_PT} on {self.mcast}:{self.mcast_port}"

    def _invite_panel(self, call, panel):
        leg = PanelLeg(call, panel)
        leg.open_audio(self.relay, call.leg_audio(leg))
        self.legs[leg.call_id] = (call, leg)
        call.legs.append(leg)
        headers = {
            "via": f"SIP/2.0/UDP {self.address}:{self.port};branch={leg.branch};rport",
            "from": f'"{call.display}" <sip:{call.door.user or "door"}@{self.address}:{self.port}>;tag={leg.from_tag}',
            "to": f"<{leg.uri}>",
            "call-id": leg.call_id,
            "cseq": f"{leg.cseq} INVITE",
            "contact": self._contact(),
            "allow": ALLOW,
        }
        leg.invite_headers = headers
        leg.state = "ringing"
        self._request("INVITE", leg.uri, headers, body=call.panel_offer(leg), addr=(panel.host, panel.port))

    def _leg_dialog_headers(self, leg, method, cseq=None):
        return {
            "via": f"SIP/2.0/UDP {self.address}:{self.port};branch={sip.new_branch()};rport",
            "from": leg.invite_headers["from"],
            "to": f"<{leg.uri}>" + (f";tag={leg.to_tag}" if leg.to_tag else ""),
            "call-id": leg.call_id,
            "cseq": f"{cseq or leg.cseq} {method}",
            "contact": self._contact(),
        }

    def _cancel_leg(self, leg):
        if leg.state not in ("ringing", "early"):
            return
        leg.state = "cancelled"
        h = {"via": leg.invite_headers["via"], "from": leg.invite_headers["from"], "to": leg.invite_headers["to"],
             "call-id": leg.call_id, "cseq": f"{leg.cseq} CANCEL"}
        self._request("CANCEL", leg.uri, h, addr=(leg.panel.host, leg.panel.port))

    def _ack_failure(self, leg, msg):
        # A non-2xx final response is ACKed on the INVITE's own branch, with the To tag it carried.
        h = {"via": leg.invite_headers["via"], "from": leg.invite_headers["from"], "to": msg.get("to"),
             "call-id": leg.call_id, "cseq": f"{leg.cseq} ACK"}
        self._request("ACK", leg.uri, h, addr=(leg.panel.host, leg.panel.port))

    def _bye_leg(self, leg):
        if leg.state != "answered":
            leg.state = "done"
            return
        leg.state = "done"
        leg.cseq += 1
        target = hostport_of(leg.remote_uri)
        self._request("BYE", leg.remote_uri, self._leg_dialog_headers(leg, "BYE"),
                      addr=target if target[0] else (leg.panel.host, leg.panel.port))

    def _door_dialog_headers(self, call, method):
        inv = call.invite
        call.our_cseq += 1
        to = inv.get("to") or ""
        return {
            "via": f"SIP/2.0/UDP {self.address}:{self.port};branch={sip.new_branch()};rport",
            "from": to + ("" if "tag=" in to else f";tag={call.local_tag}"),
            "to": inv.get("from"),
            "call-id": call.id,
            "cseq": f"{call.our_cseq} {method}",
            "contact": self._contact(),
        }

    def _bye_door(self, call):
        self._request("BYE", call.remote_uri, self._door_dialog_headers(call, "BYE"), addr=call.addr)

    def _relay_info(self, call, msg):
        """A panel's DTMF (SIP INFO) goes to the door as it came: a `*` is how a
        UniFi reader is told to unlock."""
        if call.state not in ("answering", "up"):
            return
        headers = self._door_dialog_headers(call, "INFO")
        headers["content-type"] = msg.get("content-type") or "application/dtmf-relay"
        self.note(f"{call.door.name}: DTMF from {call.answered.panel.name if call.answered else 'panel'}: "
                  f"{msg.body.decode('utf-8', 'replace').strip()[:40]!r}")
        self._request("INFO", call.remote_uri, headers, body=msg.body, addr=call.addr)

    # -- responses in (to our INVITE / CANCEL / BYE / REGISTER) --
    def _on_response(self, msg, addr):
        if self.talk and self.talk.owns(msg):
            self.talk.on_response(msg)
            return
        pair = self.legs.get(msg.call_id)
        if not pair:
            return
        call, leg = pair
        num, method = cseq_of(msg)
        st = msg.status
        if method == "INVITE":
            if st < 200:
                if leg.state == "ringing" and st >= 180:
                    leg.state = "early"
                return
            to_tag = param_of(msg.get("to"), "tag")
            if 200 <= st < 300:
                leg.to_tag = to_tag or leg.to_tag
                leg.remote_uri = uri_of(msg.get("contact")) or leg.uri
                ans = sip.sdp_parse(msg.body)
                a = ans.get("audio") or {}
                if a.get("port"):
                    leg.remote_audio = (a.get("address") or leg.panel.host, a["port"])
                # Which of the offered codecs this panel actually took. A door
                # that can pick its encoder (ours can) needs to know.
                for pt in a.get("payloads") or []:
                    if pt in (sip.G722, rtp.PCMU, rtp.PCMA):
                        leg.codec = pt
                        break
                # Always ACK a 2xx, on its own branch, to the panel's Contact.
                self._request("ACK", leg.remote_uri, self._leg_dialog_headers(leg, "ACK"),
                              addr=(leg.panel.host, leg.panel.port))
                if leg.state in ("ringing", "early") and call.state == "ringing":
                    leg.state = "answered"
                    call.answered = leg
                    call.answered_at = time.time()
                    call.state = "answering"
                    for other in call.legs:
                        if other is not leg:
                            self._cancel_leg(other)
                    self._respond(call.invite, 200, call.addr, to_tag=call.local_tag,
                                  extra={"contact": self._contact(), "allow": ALLOW},
                                  body=call.answer_sdp("sendrecv"))
                    self.note(f"{call.door.name}: {leg.panel.name} answered after "
                              f"{round(time.time() - call.started, 1)}s")
                elif leg.state == "answered":
                    return                                  # a retransmitted 200: the ACK above covers it
                else:
                    # Answered after we cancelled, or a second panel racing the first: hang it up.
                    leg.state = "answered"
                    self._bye_leg(leg)
                    leg.close(self.relay)
            else:
                self._ack_failure(leg, msg)
                if leg.state != "cancelled":
                    leg.state = "failed"
                    self.note(f"{call.door.name}: {leg.panel.name} answered {st} {msg.reason}")
                leg.close(self.relay)
                if call.state == "ringing" and all(l.state in ("failed", "cancelled", "done") for l in call.legs):
                    busy = any(l.state == "failed" for l in call.legs)
                    self._respond(call.invite, 486 if busy else 480, call.addr, to_tag=call.local_tag)
                    self._finish(call, "no panel could take the call")
        elif method == "BYE":
            leg.close(self.relay)

    # -- endings --
    def _panel_hung_up(self, call, leg):
        leg.state = "done"
        leg.close(self.relay)
        if call.answered is leg and call.state in ("answering", "up"):
            self._bye_door(call)
            self._finish(call, f"{leg.panel.name} hung up")

    def _finish(self, call, why):
        if call.state == "done":
            return
        for leg in call.legs:
            if leg.state in ("ringing", "early"):
                self._cancel_leg(leg)
            elif leg.state == "answered":
                self._bye_leg(leg)
            leg.close(self.relay)
        call.close_media()
        call.state = "done"
        call.why = why
        if not call.answered_at:
            call.answered_at = time.time()
        self.note(f"{call.door.name}: over - {why}"
                  + (f" ({call.video_in} video packets in, {call.video_out.packets} out)" if call.video_out else ""))

    # -- API surface --
    def hangup_all(self, why="hung up from the API"):
        n = 0
        with self.lock:
            for call in list(self.calls.values()):
                if call.state != "done":
                    if call.state in ("answering", "up"):
                        self._bye_door(call)
                    else:
                        self._respond(call.invite, 480, call.addr, to_tag=call.local_tag)
                    self._finish(call, why)
                    n += 1
        return n

    def status(self):
        with self.lock:
            return {
                "address": f"{self.address}:{self.port}",
                "uptime": int(time.time() - self.started),
                "panels": [p.public() for p in self.panels],
                "doors": [d.public() for d in self.doors.values()],
                "registered": self.registrar.public(),
                "calls": [c.public() for c in self.calls.values()],
                "multicast": {"group": self.mcast, "port": self.mcast_port, "ttl": self.mcast_ttl},
                "talk": self.talk.public() if self.talk else None,
                "relay": dict(self.relay.stats),
                "events": [{"t": e["t"], "text": e["text"]} for e in list(self.events)[-30:]],
            }


# -- UniFi Talk, as a third-party device --------------------------------------

class TalkClient:
    """Registers into UniFi Talk as one "third-party device" so that Talk phones
    and the Talk app can ring the house by dialing its extension. Audio only:
    whether Talk passes H.264 to its phones is unverified, and a phone is not
    where the door's picture is needed anyway. Experimental."""

    def __init__(self, bridge, cfg):
        self.b = bridge
        self.host = str(cfg.get("host")).strip()
        self.port = int(cfg.get("port") or 5060)
        self.user = str(cfg.get("user")).strip()
        self.password = str(cfg.get("password") or "")
        self.expires = max(60, int(cfg.get("expires") or 300))
        self.door = Door({"name": cfg.get("name") or "Phone", "user": self.user,
                          "password": self.password, "host": self.host, "ring": cfg.get("ring") or ["all"]})
        self.door.audio_only = True
        self.call_id = sip.new_call_id("ravabridge-talk")
        self.from_tag = sip.new_tag()
        self.cseq = 0
        self.registered_until = 0
        self.last_error = None
        self.pending_challenge = None

    def public(self):
        return {"host": self.host, "user": self.user, "registered": time.time() < self.registered_until,
                "expiresIn": max(0, int(self.registered_until - time.time())), "error": self.last_error}

    def is_peer(self, host):
        return host == self.host or host == self._resolved()

    def _resolved(self):
        try:
            return socket.gethostbyname(self.host)
        except OSError:
            return self.host

    def owns(self, msg):
        return msg.call_id == self.call_id

    def start(self):
        threading.Thread(target=self._loop, daemon=True, name="talk").start()

    def _loop(self):
        time.sleep(1)
        while True:
            try:
                self._register()
            except Exception as e:
                self.last_error = repr(e)
            # Re-register at two thirds of the granted life, or retry a failure in a minute.
            if time.time() < self.registered_until:
                wait = max(30, (self.registered_until - time.time()) * 2 / 3)
            else:
                wait = 60
            time.sleep(wait)

    def _uri(self):
        return f"sip:{self.host}:{self.port}" if self.port != 5060 else f"sip:{self.host}"

    def _register(self, auth=None):
        self.cseq += 1
        headers = {
            "via": f"SIP/2.0/UDP {self.b.address}:{self.b.port};branch={sip.new_branch()};rport",
            "from": f"<sip:{self.user}@{self.host}>;tag={self.from_tag}",
            "to": f"<sip:{self.user}@{self.host}>",
            "call-id": self.call_id,
            "cseq": f"{self.cseq} REGISTER",
            "contact": f"<sip:{self.user}@{self.b.address}:{self.b.port}>",
            "expires": str(self.expires),
        }
        if auth:
            headers["authorization"] = auth
        with self.b.lock:
            self.b._request("REGISTER", self._uri(), headers, addr=(self._resolved(), self.port))

    def on_response(self, msg):
        num, method = cseq_of(msg)
        if method != "REGISTER":
            return
        if msg.status in (401, 407):
            ch = msg.get("www-authenticate") or msg.get("proxy-authenticate")
            if self.pending_challenge == ch:
                self.last_error = "Talk rejected our credentials"
                self.pending_challenge = None
                return
            self.pending_challenge = ch
            auth = sip.digest(ch, username=self.user, password=self.password, method="REGISTER", uri=self._uri())
            self._register(auth=auth)
        elif 200 <= msg.status < 300:
            self.pending_challenge = None
            granted = None
            for c in msg.all("contact"):
                e = param_of(c, "expires")
                if e and e.isdigit():
                    granted = int(e)
            if granted is None and (msg.get("expires") or "").isdigit():
                granted = int(msg.get("expires"))
            self.registered_until = time.time() + (granted or self.expires)
            self.last_error = None
            self.b.note(f"registered with UniFi Talk as {self.user} for {granted or self.expires}s")
        else:
            self.last_error = f"{msg.status} {msg.reason}"
            self.pending_challenge = None
            self.b.note(f"UniFi Talk refused registration: {msg.status} {msg.reason}")
