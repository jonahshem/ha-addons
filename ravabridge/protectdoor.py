"""A UniFi Protect doorbell, made to ring the panels like a SIP door.

A Protect doorbell speaks no SIP. So this stands in for one: it watches the
Protect events socket, and on a press it places a SIP call INTO our own bridge
- the same bridge a real reader calls - so every panel rings with the
doorbell's picture and two-way audio, and none of the panel-side code changes.

    press (events WS) ->  INVITE 127.0.0.1:5060  ->  bridge rings the panels
    doorbell RTSP     ->  ffmpeg  ->  H.264 + G.711 RTP  ->  bridge -> panels
    panel voice       ->  bridge  ->  G.711  ->  ffmpeg  ->  Opus -> talkback

Media is ffmpeg's job, because the panels speak G.711 and H.264 while a Protect
doorbell speaks AAC over RTSP and wants Opus for talkback - conversions no
stdlib can do. Video is copied through untouched; only audio is transcoded.

The one wrinkle worth naming: the bridge learns where to send the panel's voice
from the *source* of the doorbell's audio (symmetric RTP), so the downstream
audio has to leave from the very port the talkback comes back on. That is why
audio goes through one socket of ours (`_AudioLeg`) rather than straight out of
ffmpeg, while video, which the bridge never sends back, goes out of ffmpeg
directly.

Standard library only (ffmpeg is an external process, not an import).
"""
import os
import re
import shutil
import socket
import subprocess
import tempfile
import threading
import time

import protect
import rtp
import sip


def _free_udp_port():
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.bind(("0.0.0.0", 0))
        return s.getsockname()[1]
    finally:
        s.close()


def slugify(name):
    return re.sub(r"[^a-z0-9]+", "-", str(name).lower()).strip("-") or "protect-door"


def downstream_cmd(ffmpeg, rtsp_url, video=None, audio=None):
    """The ffmpeg that carries a camera: video copied as-is, audio to G.711.

    `video` and `audio` are (host, port) - or (host, port, ttl) for a multicast
    video target. One function, so a probe exercises exactly what a call runs.
    """
    outs = []
    if video:
        ttl = f"&ttl={video[2]}" if len(video) > 2 and video[2] else ""
        outs += ["-map", "0:v:0", "-c:v", "copy", "-an", "-payload_type", "96",
                 "-f", "rtp", f"rtp://{video[0]}:{video[1]}?pkt_size=1200{ttl}"]
    if audio:
        outs += ["-map", "0:a:0", "-vn", "-c:a", "pcm_mulaw", "-ar", "8000", "-ac", "1",
                 "-payload_type", "0", "-f", "rtp", f"rtp://{audio[0]}:{audio[1]}"]
    return [ffmpeg, "-hide_banner", "-loglevel", "warning", "-fflags", "nobuffer",
            "-rtsp_transport", "tcp", "-i", rtsp_url] + outs


class _AudioLeg:
    """One UDP socket that is the door's audio, both ways.

    Bound to the port advertised in the SIP offer. ffmpeg's transcoded G.711
    is forwarded out of it to the bridge (so the bridge's symmetric RTP learns
    this port), and whatever the bridge sends back here is forwarded on to the
    talkback encoder.
    """

    def __init__(self, listen_port, from_ffmpeg_port, log=print):
        self.port = listen_port
        self.from_ffmpeg_port = from_ffmpeg_port
        self.log = log
        self.bridge_audio = None            # (host, port) the bridge answered with
        self.talkback_in = None             # (host, port) of the ffmpeg-up input
        self._stop = threading.Event()
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.sock.bind(("0.0.0.0", self.port))
        self.sock.settimeout(0.5)
        self.rx = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.rx.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.rx.bind(("127.0.0.1", self.from_ffmpeg_port))
        self.rx.settimeout(0.5)
        self.down = 0
        self.up = 0

    def start(self):
        threading.Thread(target=self._downstream, daemon=True, name="protect-audio-down").start()
        threading.Thread(target=self._upstream, daemon=True, name="protect-audio-up").start()

    def _downstream(self):
        # ffmpeg's G.711 out -> the bridge, from our advertised port.
        while not self._stop.is_set():
            try:
                data, _ = self.rx.recvfrom(4096)
            except socket.timeout:
                continue
            except OSError:
                break
            if self.bridge_audio:
                try:
                    self.sock.sendto(data, self.bridge_audio)
                    self.down += 1
                except OSError:
                    pass

    def _upstream(self):
        # The panel's voice from the bridge -> the talkback encoder.
        while not self._stop.is_set():
            try:
                data, _ = self.sock.recvfrom(4096)
            except socket.timeout:
                continue
            except OSError:
                break
            if self.talkback_in:
                try:
                    self.rx.sendto(data, self.talkback_in)
                    self.up += 1
                except OSError:
                    pass

    def stop(self):
        self._stop.set()
        for s in (self.sock, self.rx):
            try:
                s.close()
            except OSError:
                pass


class ProtectCall:
    """One doorbell press, from INVITE to hang-up."""

    def __init__(self, mgr, doorbell, camera_id, log=print):
        self.mgr = mgr
        self.bell = doorbell
        self.camera_id = camera_id
        self.log = log
        self.b = mgr.bridge
        self.call_id = sip.new_call_id("protect")
        self.from_tag = sip.new_tag()
        self.branch = sip.new_branch()
        self.done = threading.Event()
        self.answered = False
        self.ffdown = None
        self.ffup = None
        self.audio = None
        self._sdp_tmp = None
        self.state = "new"

    # -- SIP UAC over loopback --------------------------------------------
    def _sig_target(self):
        return ("127.0.0.1", self.b.port)

    def _local_contact(self, port):
        return f"<sip:{self.bell['user']}@127.0.0.1:{port}>"

    def _offer(self, audio_port, video_port):
        fmtp = self.bell.get("video_fmtp") or "profile-level-id=42801f;packetization-mode=1"
        me = self.b.address
        lines = [
            "v=0", f"o=protect {int(time.time())} {int(time.time())} IN IP4 {me}",
            "s=ProtectDoor", f"c=IN IP4 {me}", "t=0 0",
            f"m=audio {audio_port} RTP/AVP 0 101",
            "a=rtpmap:0 PCMU/8000", "a=rtpmap:101 telephone-event/8000",
            "a=fmtp:101 0-15", "a=ptime:20", "a=sendrecv",
            f"m=video {video_port} RTP/AVP 96", "a=rtpmap:96 H264/90000",
            f"a=fmtp:96 {fmtp}", "a=sendrecv",
        ]
        return ("\r\n".join(lines) + "\r\n").encode("ascii")

    def run(self):
        try:
            self._run()
        except Exception as e:
            self.log(f"protect call {self.bell['name']}: {e!r}")
        finally:
            self._cleanup()

    def _run(self):
        client = self.mgr.client
        # 1) A PLAY-able RTSP URL for the doorbell (turn RTSP on if it is off).
        rtsp_url, quality = client.stream_url(self.camera_id, self.bell.get("quality", "high"),
                                              enable=self.bell.get("enable_rtsp", True))
        if not rtsp_url:
            self.log(f"protect call {self.bell['name']}: the doorbell has no RTSP stream and one could not be enabled")
            return
        # 2) The SIP call into our own bridge.
        sig = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sig.bind(("127.0.0.1", 0))
        sig.settimeout(0.5)
        my_port = sig.getsockname()[1]
        audio_port = _free_udp_port()
        video_port = _free_udp_port()
        from_ffmpeg = _free_udp_port()
        self.audio = _AudioLeg(audio_port, from_ffmpeg, self.log)
        self.audio.start()

        invite = sip.Message(method="INVITE", uri=f"sip:{self.bell['user']}@{self.b.address}", headers={
            "via": f"SIP/2.0/UDP 127.0.0.1:{my_port};branch={self.branch}",
            "from": f"\"{self.bell['name']}\" <sip:{self.bell['user']}@127.0.0.1>;tag={self.from_tag}",
            "to": f"<sip:{self.bell['user']}@{self.b.address}>",
            "call-id": self.call_id, "cseq": "1 INVITE",
            "contact": self._local_contact(my_port),
            "max-forwards": "70", "user-agent": "RavaBridge-Protect",
            "content-type": "application/sdp",
        }, body=self._offer(audio_port, video_port))
        sig.sendto(sip.build(invite), self._sig_target())
        self.state = "inviting"
        self.log(f"protect: {self.bell['name']} pressed -> ringing the panels ({quality})")

        answer = None
        to_tag = None
        deadline = time.time() + self.mgr.ring_seconds + 10
        while time.time() < deadline and not self.done.is_set():
            try:
                data, _ = sig.recvfrom(65535)
            except socket.timeout:
                continue
            msg = sip.parse(data)
            if msg is None or msg.call_id != self.call_id:
                # A BYE from the bridge is a new transaction but same Call-ID.
                if msg and msg.is_request and msg.method == "BYE" and msg.call_id == self.call_id:
                    pass
                else:
                    continue
            if msg.is_request:
                if msg.method == "BYE":
                    self._ok(sig, msg)
                    self.log(f"protect: {self.bell['name']} call ended by the house")
                    break
                if msg.method in ("INFO", "OPTIONS", "UPDATE", "NOTIFY"):
                    self._ok(sig, msg)     # DTMF/unlock could be mapped here later
                    continue
                continue
            num, method = _cseq(msg)
            if method != "INVITE":
                continue
            st = msg.status
            if st < 200:
                if st >= 180 and msg.body:
                    answer = answer or sip.sdp_parse(msg.body)
                    if answer and not self.ffdown:
                        self._start_downstream(rtsp_url, answer)   # early media: picture while ringing
                continue
            if 200 <= st < 300:
                to_tag = sip.params(msg.get("to")).get("tag")
                if msg.body:
                    answer = sip.sdp_parse(msg.body) or answer
                self._ack(sig, msg, to_tag)
                if not self.ffdown and answer:
                    self._start_downstream(rtsp_url, answer)
                self.answered = True
                self.state = "up"
                self._start_talkback()
                self.log(f"protect: {self.bell['name']} answered on a panel")
                self._wait_for_bye(sig)
                break
            else:
                self.log(f"protect: {self.bell['name']} not connected ({st} {msg.reason})")
                break

        if self.state == "inviting" and not self.answered:
            self._cancel(sig)

    def _wait_for_bye(self, sig):
        limit = time.time() + self.mgr.max_call_seconds
        while time.time() < limit and not self.done.is_set():
            try:
                data, _ = sig.recvfrom(65535)
            except socket.timeout:
                if self.ffdown and self.ffdown.poll() is not None:
                    self.log(f"protect: {self.bell['name']} media stopped; ending")
                    self._bye(sig)
                    return
                continue
            msg = sip.parse(data)
            if msg is None or msg.call_id != self.call_id:
                continue
            if msg.is_request and msg.method == "BYE":
                self._ok(sig, msg)
                return
            if msg.is_request and msg.method in ("INFO", "OPTIONS", "UPDATE", "NOTIFY"):
                self._ok(sig, msg)
        self._bye(sig)

    def _ack(self, sig, resp, to_tag):
        ack = sip.Message(method="ACK", uri=f"sip:{self.bell['user']}@{self.b.address}", headers={
            "via": f"SIP/2.0/UDP 127.0.0.1:{sig.getsockname()[1]};branch={sip.new_branch()}",
            "from": resp.get("from"), "to": resp.get("to"),
            "call-id": self.call_id, "cseq": "1 ACK", "max-forwards": "70",
        })
        sig.sendto(sip.build(ack), self._sig_target())

    def _bye(self, sig):
        bye = sip.Message(method="BYE", uri=f"sip:{self.bell['user']}@{self.b.address}", headers={
            "via": f"SIP/2.0/UDP 127.0.0.1:{sig.getsockname()[1]};branch={sip.new_branch()}",
            "from": f"\"{self.bell['name']}\" <sip:{self.bell['user']}@127.0.0.1>;tag={self.from_tag}",
            "to": f"<sip:{self.bell['user']}@{self.b.address}>",
            "call-id": self.call_id, "cseq": "2 BYE", "max-forwards": "70",
        })
        try:
            sig.sendto(sip.build(bye), self._sig_target())
        except OSError:
            pass

    def _cancel(self, sig):
        cancel = sip.Message(method="CANCEL", uri=f"sip:{self.bell['user']}@{self.b.address}", headers={
            "via": f"SIP/2.0/UDP 127.0.0.1:{sig.getsockname()[1]};branch={self.branch}",
            "from": f"\"{self.bell['name']}\" <sip:{self.bell['user']}@127.0.0.1>;tag={self.from_tag}",
            "to": f"<sip:{self.bell['user']}@{self.b.address}>",
            "call-id": self.call_id, "cseq": "1 CANCEL", "max-forwards": "70",
        })
        try:
            sig.sendto(sip.build(cancel), self._sig_target())
        except OSError:
            pass

    def _ok(self, sig, req):
        to = req.get("to") or ""
        headers = {"via": req.all("via"), "from": req.get("from"), "to": to,
                   "call-id": req.call_id, "cseq": req.get("cseq")}
        sig.sendto(sip.build(sip.Message(status=200, headers=headers)), self._sig_target())

    # -- media ------------------------------------------------------------
    def _start_downstream(self, rtsp_url, answer):
        a = answer.get("audio") or {}
        v = answer.get("video") or {}
        if not a.get("port"):
            self.log(f"protect: {self.bell['name']} - the bridge offered no audio port")
            return
        self.audio.bridge_audio = (a.get("address") or self.b.address, a["port"])
        video = None
        if v.get("port"):
            # The bridge's c= for video may be `group/ttl`; ffmpeg wants the ttl apart.
            host = v.get("address") or self.b.address
            video = (host.split("/")[0], v["port"], host.split("/")[1] if "/" in host else "")
        cmd = downstream_cmd(self.mgr.ffmpeg, rtsp_url, video=video,
                             audio=("127.0.0.1", self.audio.from_ffmpeg_port))
        self.ffdown = self._spawn(cmd, "ffmpeg-down")

    def _start_talkback(self):
        if not self.bell.get("talkback", True):
            return
        try:
            sess = self.mgr.client.talkback_session(self.camera_id)
        except Exception as e:
            self.log(f"protect: {self.bell['name']} - no talkback session: {e}")
            return
        url = (sess or {}).get("url")
        if not url:
            self.log(f"protect: {self.bell['name']} - talkback session returned no url")
            return
        codec = (sess.get("codec") or "opus").lower()
        rate = int(sess.get("samplingRate") or 24000)
        enc = "libopus" if codec == "opus" else ("aac" if codec == "aac" else codec)
        pt = 111 if codec == "opus" else 97
        # ffmpeg reads the panel's G.711 (delivered to this port by _AudioLeg).
        in_port = _free_udp_port()
        self.audio.talkback_in = ("127.0.0.1", in_port)
        sdp = ("v=0\r\no=- 0 0 IN IP4 127.0.0.1\r\ns=talkback\r\nc=IN IP4 127.0.0.1\r\n"
               f"t=0 0\r\nm=audio {in_port} RTP/AVP 0\r\na=rtpmap:0 PCMU/8000\r\n")
        fd, path = tempfile.mkstemp(suffix=".sdp")
        os.write(fd, sdp.encode("ascii"))
        os.close(fd)
        self._sdp_tmp = path
        cmd = [self.mgr.ffmpeg, "-hide_banner", "-loglevel", "warning",
               "-protocol_whitelist", "file,udp,rtp", "-i", path,
               "-c:a", enc, "-ar", str(rate), "-ac", "1", "-payload_type", str(pt),
               "-f", "rtp", url]
        self.ffup = self._spawn(cmd, "ffmpeg-up")
        self.log(f"protect: {self.bell['name']} talkback -> {url} ({codec}/{rate})")

    def _spawn(self, cmd, tag):
        try:
            p = subprocess.Popen(cmd, stdin=subprocess.DEVNULL,
                                 stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        except FileNotFoundError:
            self.log(f"protect: ffmpeg not found; cannot carry {tag}")
            return None
        threading.Thread(target=self._drain, args=(p, tag), daemon=True).start()
        return p

    def _drain(self, p, tag):
        for raw in iter(p.stderr.readline, b""):
            line = raw.decode("utf-8", "replace").rstrip()
            if line:
                self.log(f"[{tag}] {line}")

    def _cleanup(self):
        self.done.set()
        for p in (self.ffdown, self.ffup):
            if p and p.poll() is None:
                try:
                    p.terminate()
                    p.wait(timeout=3)
                except Exception:
                    try:
                        p.kill()
                    except Exception:
                        pass
        if self.audio:
            self.audio.stop()
        if self._sdp_tmp:
            try:
                os.unlink(self._sdp_tmp)
            except OSError:
                pass
        self.state = "done"
        self.mgr._call_over(self)


DOORBELL_WORDS = ("doorbell", "entry", "chime")
OBJECT_TRIGGERS = ("person", "vehicle", "animal", "package", "licenseplate", "face")


class ProtectDoors:
    """Every camera on a Protect console, any of which can ring the panels.

    All cameras are discovered and listed (like Access door discovery). A
    doorbell is switched on by default and calls on **ring**. Any other camera
    can be switched on too, with its trigger chosen from what Protect offers for
    that camera: motion, a smart detection (person, vehicle, animal, package,
    licensePlate, face), a line crossing, loitering, or an audio alarm. What is
    found is written back into the add-on's options so it shows on the
    Configuration page ready to flip; an operator's edits are never overwritten.
    """

    def __init__(self, bridge, cfg, log=print):
        self.bridge = bridge
        self.log = log
        pc = cfg.get("protect") or {}
        self.host = str(pc.get("host") or "").strip()
        self.api_key = str(pc.get("api_key") or "").strip()
        self.autodetect = bool(pc.get("autodetect", True))
        self.debug = bool(pc.get("debug"))
        self.default_ring = [str(r).strip().lower() for r in (pc.get("ring") or ["all"]) if str(r).strip()] or ["all"]
        self.ring_seconds = int(pc.get("ring_seconds") or bridge.ring_seconds or 60)
        self.max_call_seconds = int(pc.get("max_call_seconds") or 3600)
        self.ffmpeg = shutil.which("ffmpeg") or "ffmpeg"
        self.cameras = []
        for c in pc.get("cameras") or []:
            if isinstance(c, dict):
                self.cameras.append(self._entry(c))
        for d in pc.get("doorbells") or []:            # the 0.6 shape, still honoured
            if not isinstance(d, dict):
                continue
            e = self._entry(d, call=True)
            e["triggers"] = e["triggers"] or ["ring"]
            if not self._known(e["camera_id"], e["name"]):
                self.cameras.append(e)
        self.client = None
        self.events = None
        self._active = None
        self._lock = threading.Lock()
        self._by_camera = {}          # camera_id -> entry
        self._recent = {}             # camera_id -> last call time (cooldown)
        self._fired = {}              # Protect event id -> time (fire once per event)
        self.enabled = bool(self.host and self.api_key)

    # -- the camera model ---------------------------------------------------
    def _entry(self, c, call=None):
        name = str(c.get("name") or c.get("camera_name") or "Camera")
        if c.get("call") is not None:
            call = bool(c.get("call"))
        return {
            "name": name, "user": slugify(c.get("user") or name),
            "camera_id": str(c.get("camera_id") or "").strip(),
            "camera_name": str(c.get("camera_name") or "").strip(),
            "call": bool(call) if call is not None else None,
            "triggers": [str(t).strip().lower() for t in (c.get("triggers") or []) if str(t).strip()],
            "ring": [str(r).strip().lower() for r in (c.get("ring") or []) if str(r).strip()] or list(self.default_ring),
            "quality": str(c.get("quality") or "high"),
            "talkback": c.get("talkback"),               # None: decided by the camera having a speaker
            "enable_rtsp": bool(c.get("enable_rtsp", True)),
            "cooldown": c.get("cooldown_seconds"),
            "type": str(c.get("type") or ""),
            "available": [str(a) for a in (c.get("available") or [])],
        }

    def _known(self, camera_id, name):
        for e in self.cameras:
            if (camera_id and e["camera_id"] == camera_id) or e["name"].lower() == str(name or "").lower():
                return e
        return None

    @staticmethod
    def _is_doorbell(cam):
        """By model type - the integration API exposes no doorbell flag. G4
        Doorbell, G4 Doorbell Pro, G6 Entry... UA-* devices are UniFi Access
        readers, which reach the panels over their own SIP path: never here."""
        t = str(cam.get("type") or "").lower()
        if t.startswith("ua"):
            return False
        return any(w in t for w in DOORBELL_WORDS)

    @staticmethod
    def _available(cam, detail):
        ff = (detail or {}).get("featureFlags") or {}
        out = ["ring"] if ProtectDoors._is_doorbell(cam) else []
        out.append("motion")
        smart = [str(x) for x in ff.get("smartDetectTypes") or []]
        out += smart
        if smart:
            out += ["line", "loiter"]
        out += [str(x) for x in ff.get("smartDetectAudioTypes") or []]
        return out

    # -- lifecycle ------------------------------------------------------------
    def start(self):
        if not self.enabled:
            return
        self.client = protect.ProtectClient(self.host, self.api_key)
        cams, details = [], {}
        try:
            cams = self.client.cameras()
        except Exception as e:
            self.log(f"protect: could not list cameras: {e}")
        for cam in cams:
            try:
                details[cam.get("id")] = self.client.camera(cam.get("id"))
            except Exception as e:
                self.log(f"protect: no detail for {cam.get('name')}: {e}")
        if self.autodetect:
            self._autodetect(cams)
        self._finalize(cams, details)
        self._register_doors()
        if cams:
            self._persist()
        self.events = protect.EventsSocket(self.host, self.api_key, self._on_event, self.log)
        self.events.start()
        calling = [f"{e['name']} on {','.join(e['triggers']) or '-'}" for e in self.cameras if e["call"]]
        self.log(f"protect: {len(self.cameras)} camera(s) on {self.host}; calling the panels: "
                 + (", ".join(calling) if calling else "none yet")
                 + f"; ffmpeg {'found' if shutil.which('ffmpeg') else 'MISSING - audio/video will not flow'}")

    def _autodetect(self, cams):
        for cam in cams:
            if self._known(cam.get("id"), cam.get("name")):
                continue
            bell = self._is_doorbell(cam)
            e = self._entry({"name": cam.get("name") or "Camera", "camera_id": cam.get("id"),
                             "camera_name": cam.get("name") or ""}, call=bell)
            e["triggers"] = ["ring"] if bell else []
            self.cameras.append(e)
            self.log(f"protect: found {'doorbell' if bell else 'camera'} '{cam.get('name')}' ({cam.get('type')})"
                     + (" - will call the panels on ring" if bell else " - off until `call` is set"))

    def _finalize(self, cams, details):
        by_id = {c.get("id"): c for c in cams}
        by_name = {str(c.get("name", "")).lower(): c for c in cams}
        for e in self.cameras:
            cam = by_id.get(e["camera_id"]) or by_name.get(e["camera_name"].lower()) or by_name.get(e["name"].lower())
            if cam:
                e["camera_id"] = cam.get("id")
                e["camera_name"] = e["camera_name"] or str(cam.get("name") or "")
                e["type"] = str(cam.get("type") or e["type"])
                e["available"] = self._available(cam, details.get(cam.get("id")))
            elif not e["camera_id"]:
                self.log(f"protect: '{e['name']}' names no camera that exists on {self.host}")
            bell = cam is not None and self._is_doorbell(cam)
            if e["call"] is None:
                e["call"] = bell
            if e["call"] and not e["triggers"]:
                e["triggers"] = ["ring"] if bell else ["motion"]
            if e["talkback"] is None:
                ff = (details.get(e["camera_id"]) or {}).get("featureFlags") or {}
                e["talkback"] = bool(ff.get("hasSpeaker")) if ff else bell
            if e["cooldown"] is None:
                e["cooldown"] = 3 if "ring" in e["triggers"] else 60
            if e["camera_id"]:
                self._by_camera[e["camera_id"]] = e

    def _register_doors(self):
        """Every camera is a Door the bridge can ring for, callable over loopback."""
        import bridge as bridgemod
        for e in self.cameras:
            self.bridge.doors[e["user"]] = bridgemod.Door({
                "name": e["name"], "user": e["user"], "host": "127.0.0.1", "ring": e["ring"]})
        self.bridge.registrar.users = {u: d.password for u, d in self.bridge.doors.items()}

    def _persist(self):
        """Write the camera list back into the add-on options (Supervisor), so the
        Configuration page shows every camera with `call`/`triggers` ready to edit.
        Adds and fills in; never removes or overrides what a person typed."""
        try:
            import discover
            options, token = discover.supervisor_options()
        except Exception as e:
            self.log(f"protect: cannot read the options to save cameras: {e!r}")
            return
        if options is None or not token:
            return
        pc = options.setdefault("protect", {})
        existing = pc.get("cameras") or []
        by_key = {}
        for c in existing:
            if isinstance(c, dict):
                by_key[str(c.get("camera_id") or "")] = c
                by_key["n:" + str(c.get("name") or "").lower()] = c
        changed = False
        for e in self.cameras:
            row = by_key.get(e["camera_id"]) or by_key.get("n:" + e["name"].lower())
            if row is None:
                row = {"name": e["name"], "camera_id": e["camera_id"], "camera_name": e["camera_name"],
                       "call": bool(e["call"]), "triggers": list(e["triggers"]), "ring": list(e["ring"]),
                       "talkback": bool(e["talkback"]), "quality": e["quality"]}
                existing.append(row)
                changed = True
            # Informational, refreshed each start; the operator's own fields are left alone.
            for k in ("type", "available"):
                if row.get(k) != e[k]:
                    row[k] = e[k]
                    changed = True
            if not row.get("camera_id") and e["camera_id"]:
                row["camera_id"] = e["camera_id"]
                changed = True
        pc["cameras"] = existing
        if pc.get("doorbells"):
            pc["doorbells"] = []                 # migrated into cameras above
            changed = True
        if not changed:
            return
        try:
            discover.supervisor_write(options, token)
            self.log(f"protect: {len(existing)} camera(s) saved to the options page")
        except Exception as e:
            body = getattr(e, "read", lambda: b"")()
            self.log(f"protect: could not save cameras to the options: {e!r} "
                     f"{body[:200].decode('utf-8', 'replace') if body else ''}")

    # -- events ---------------------------------------------------------------
    def _on_event(self, kind, item):
        etype = str(item.get("type") or "").lower()
        cam_id = item.get("device") or item.get("camera") or item.get("cameraId")
        if isinstance(cam_id, dict):
            cam_id = cam_id.get("id")
        if not cam_id or not etype:
            return
        entry = self._by_camera.get(cam_id)
        if self.debug:
            self.log(f"protect event: {etype} from {entry['name'] if entry else cam_id} "
                     f"{item.get('smartDetectTypes') or ''}".rstrip())
        if entry is None and etype == "ring" and self.autodetect:
            # It rang, so it is a doorbell we did not know about. Adopt it.
            name = f"Doorbell {cam_id[-4:]}"
            entry = self._entry({"name": name, "camera_id": cam_id}, call=True)
            entry.update(triggers=["ring"], talkback=True, cooldown=3, type="", available=["ring", "motion"])
            self.cameras.append(entry)
            self._by_camera[cam_id] = entry
            self._register_doors()
            self.log(f"protect: adopted '{name}' on its first ring")
        if entry is None or not entry["call"]:
            return
        hit = self._matches(entry, etype, item.get("smartDetectTypes") or [])
        if not hit:
            return
        ev_id = str(item.get("id") or "")
        now = time.time()
        if ev_id:
            self._fired = {k: t for k, t in self._fired.items() if now - t < 600}
            if ev_id in self._fired:
                return                          # the same Protect event, updated
            self._fired[ev_id] = now
        if now - self._recent.get(cam_id, 0) < float(entry["cooldown"] or 0):
            return
        self._recent[cam_id] = now
        self.log(f"protect: {entry['name']}: {hit} -> calling the panels")
        self._ring(entry, cam_id)

    @staticmethod
    def _matches(entry, etype, smart):
        smart = [str(s).lower() for s in smart]
        kinds = {"line": "smartdetectline", "loiter": "smartdetectloiterzone"}
        for t in entry["triggers"]:
            if t == "ring" and etype == "ring":
                return t
            if t == "motion" and etype == "motion":
                return t
            if t in kinds and etype == kinds[t]:
                return t
            if ":" in t:                                # line:person, loiter:vehicle
                k, _, obj = t.partition(":")
                if etype == kinds.get(k) and obj in smart:
                    return t
            if t in OBJECT_TRIGGERS and etype == "smartdetectzone" and t in smart:
                return t
            if t.startswith("alrm") and etype == "smartaudiodetect" and t in smart:
                return t
        return None

    def _ring(self, entry, cam_id):
        with self._lock:
            if self._active and self._active.state != "done":
                self.log(f"protect: {entry['name']} triggered while a call is up; ignoring")
                return
            if entry["user"] not in self.bridge.doors:
                self._register_doors()
            call = ProtectCall(self, entry, cam_id or entry.get("camera_id"), self.log)
            self._active = call
        threading.Thread(target=call.run, name="protect-call", daemon=True).start()

    def _call_over(self, call):
        with self._lock:
            if self._active is call:
                self._active = None

    def ring_now(self, name_or_user):
        """Manual trigger (API /protectring), for any camera, call switched on or not."""
        key = slugify(name_or_user)
        for e in self.cameras:
            if e["user"] == key or slugify(e["name"]) == key or slugify(e["camera_name"] or "") == key:
                self._ring(e, e.get("camera_id"))
                return True
        return False

    def probe(self, name_or_user, seconds=8):
        """Pull a camera's media exactly as a call would, into throwaway sinks.

        Proves the picture and the voice really flow, and at what rate, with no
        SIP call in it: nothing rings. The safe way to check a camera.
        """
        key = slugify(name_or_user)
        entry = next((e for e in self.cameras
                      if e["user"] == key or slugify(e["name"]) == key
                      or slugify(e["camera_name"] or "") == key), None)
        if entry is None:
            return {"error": f"no Protect camera called {name_or_user!r}"}
        if not self.client:
            return {"error": "the Protect console is not configured"}
        seconds = max(2, min(30, int(seconds or 8)))
        try:
            url, quality = self.client.stream_url(entry["camera_id"], entry["quality"],
                                                  enable=entry["enable_rtsp"])
        except Exception as e:
            return {"error": f"could not get an RTSP stream: {e}"}
        if not url:
            return {"error": "the camera has no RTSP stream and one could not be enabled"}

        socks = {}
        for kind in ("video", "audio"):
            sk = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            sk.bind(("127.0.0.1", 0))
            sk.settimeout(0.5)
            socks[kind] = sk
        cmd = downstream_cmd(self.ffmpeg, url,
                             video=("127.0.0.1", socks["video"].getsockname()[1]),
                             audio=("127.0.0.1", socks["audio"].getsockname()[1]))
        stats = {k: {"packets": 0, "bytes": 0, "pt": None, "first": None, "last": None,
                     "gaps": 0, "prev": None} for k in socks}
        errs = []
        try:
            proc = subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                    stderr=subprocess.PIPE)
        except FileNotFoundError:
            for sk in socks.values():
                sk.close()
            return {"error": "ffmpeg is not installed in this add-on"}

        def drain():
            for raw in iter(proc.stderr.readline, b""):
                line = raw.decode("utf-8", "replace").strip()
                if line:
                    errs.append(line[:200])
        threading.Thread(target=drain, daemon=True).start()

        stop = threading.Event()

        def collect(kind):
            sk, st = socks[kind], stats[kind]
            while not stop.is_set():
                try:
                    data, _ = sk.recvfrom(65535)
                except socket.timeout:
                    continue
                except OSError:
                    break
                now = time.time()
                st["packets"] += 1
                st["bytes"] += len(data)
                pkt = rtp.unpack(data)
                if pkt:
                    st["pt"] = pkt["pt"]
                    seq = pkt.get("seq")
                    if st["prev"] is not None and seq is not None and seq != (st["prev"] + 1) % 65536:
                        st["gaps"] += 1
                    st["prev"] = seq
                if st["first"] is None:
                    st["first"] = now
                st["last"] = now

        started = time.time()
        for k in socks:
            threading.Thread(target=collect, args=(k,), daemon=True).start()
        time.sleep(seconds)
        stop.set()
        try:
            proc.terminate()
            proc.wait(timeout=3)
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass
        time.sleep(0.3)
        for sk in socks.values():
            try:
                sk.close()
            except OSError:
                pass

        out = {"camera": entry["name"], "quality": quality, "seconds": seconds,
               "rang": False, "ffmpegErrors": errs[-4:]}
        for kind, st in stats.items():
            span = (st["last"] - st["first"]) if st["first"] and st["last"] and st["last"] > st["first"] else 0
            out[kind] = {"packets": st["packets"], "bytes": st["bytes"], "payloadType": st["pt"],
                         "perSecond": round(st["packets"] / span, 1) if span else 0,
                         "startedAfter": round(st["first"] - started, 2) if st["first"] else None,
                         "streamedFor": round(span, 1), "sequenceGaps": st["gaps"]}
        out["ok"] = stats["video"]["packets"] > 0 and stats["audio"]["packets"] > 0
        # G.711 at 20 ms is 50 packets a second; well under that is a stuttering voice.
        out["audioSteady"] = bool(out["audio"]["perSecond"] >= 45)
        return out

    def public(self):
        return {"enabled": self.enabled, "host": self.host, "autodetect": self.autodetect,
                "ffmpeg": bool(shutil.which("ffmpeg")),
                "cameras": [{"name": e["name"], "type": e["type"], "camera_id": e["camera_id"],
                             "call": bool(e["call"]), "triggers": e["triggers"], "available": e["available"],
                             "ring": e["ring"], "talkback": bool(e["talkback"])} for e in self.cameras],
                "active": self._active.state if self._active else None,
                "debug": self.debug}


def _cseq(msg):
    raw = (msg.get("cseq") or "").split()
    try:
        return int(raw[0]), (raw[1].upper() if len(raw) > 1 else "")
    except (ValueError, IndexError):
        return 0, ""
