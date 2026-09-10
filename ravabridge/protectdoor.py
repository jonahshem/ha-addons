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
        ba = (a.get("address") or self.b.address, a.get("port"))
        bv = (v.get("address") or self.b.address, v.get("port"))
        if not a.get("port"):
            self.log(f"protect: {self.bell['name']} - the bridge offered no audio port")
            return
        self.audio.bridge_audio = ba
        outs = []
        if v.get("port"):
            # Split the multicast address (c= can be group/ttl); ffmpeg wants ttl as a param.
            vhost = bv[0].split("/")[0]
            vttl = ""
            if "/" in bv[0]:
                vttl = "&ttl=" + bv[0].split("/")[1]
            outs += ["-map", "0:v:0", "-c:v", "copy", "-an", "-payload_type", "96",
                     "-f", "rtp", f"rtp://{vhost}:{bv[1]}?pkt_size=1200{vttl}"]
        outs += ["-map", "0:a:0", "-vn", "-c:a", "pcm_mulaw", "-ar", "8000", "-ac", "1",
                 "-payload_type", "0", "-f", "rtp", f"rtp://127.0.0.1:{self.audio.from_ffmpeg_port}"]
        cmd = [self.mgr.ffmpeg, "-hide_banner", "-loglevel", "warning", "-fflags", "nobuffer",
               "-rtsp_transport", "tcp", "-i", rtsp_url] + outs
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


class ProtectDoors:
    """Watches a Protect console and rings the panels when a doorbell is pressed."""

    def __init__(self, bridge, cfg, log=print):
        self.bridge = bridge
        self.log = log
        pc = cfg.get("protect") or {}
        self.host = str(pc.get("host") or "").strip()
        self.api_key = str(pc.get("api_key") or "").strip()
        self.doorbells = []
        for d in pc.get("doorbells") or []:
            if not isinstance(d, dict):
                continue
            name = str(d.get("name") or "Front Door")
            self.doorbells.append({
                "name": name, "user": slugify(d.get("user") or name),
                "camera_id": str(d.get("camera_id") or "").strip(),
                "camera_name": str(d.get("camera_name") or "").strip(),
                "ring": [str(r).strip().lower() for r in (d.get("ring") or ["all"]) if str(r).strip()],
                "quality": str(d.get("quality") or "high"),
                "talkback": bool(d.get("talkback", True)),
                "enable_rtsp": bool(d.get("enable_rtsp", True)),
            })
        self.ring_seconds = int(pc.get("ring_seconds") or bridge.ring_seconds or 60)
        self.max_call_seconds = int(pc.get("max_call_seconds") or 3600)
        self.ffmpeg = shutil.which("ffmpeg") or "ffmpeg"
        self.client = None
        self.events = None
        self._active = None
        self._lock = threading.Lock()
        self._by_camera = {}          # camera_id -> doorbell
        self._recent = {}             # camera_id -> last ring time (debounce)
        self.enabled = bool(self.host and self.api_key and self.doorbells)

    def start(self):
        if not self.enabled:
            return
        self.client = protect.ProtectClient(self.host, self.api_key)
        self._register_doors()
        try:
            self._resolve_cameras()
        except Exception as e:
            self.log(f"protect: could not list cameras: {e}; will match rings by id only")
        self.events = protect.EventsSocket(self.host, self.api_key, self._on_event, self.log)
        self.events.start()
        self.log(f"protect: watching {self.host} for {len(self.doorbells)} doorbell(s); "
                 f"ffmpeg {'found' if shutil.which('ffmpeg') else 'MISSING - audio/video will not flow'}")

    def _register_doors(self):
        """Make each doorbell a Door the bridge will ring, callable over loopback."""
        import bridge as bridgemod
        for d in self.doorbells:
            self.bridge.doors[d["user"]] = bridgemod.Door({
                "name": d["name"], "user": d["user"], "host": "127.0.0.1", "ring": d["ring"],
            })
        self.bridge.registrar.users = {u: dr.password for u, dr in self.bridge.doors.items()}

    def _resolve_cameras(self):
        cams = self.client.cameras()
        by_id = {c.get("id"): c for c in cams}
        by_name = {str(c.get("name", "")).lower(): c for c in cams}
        for d in self.doorbells:
            cam = None
            if d["camera_id"] and d["camera_id"] in by_id:
                cam = by_id[d["camera_id"]]
            elif d["camera_name"] and d["camera_name"].lower() in by_name:
                cam = by_name[d["camera_name"].lower()]
            if cam:
                d["camera_id"] = cam.get("id")
                self._by_camera[cam.get("id")] = d
                self.log(f"protect: '{d['name']}' -> camera {cam.get('name')} ({cam.get('id')})")
            elif d["camera_id"]:
                self._by_camera[d["camera_id"]] = d
            else:
                self.log(f"protect: '{d['name']}' names no camera that exists on {self.host}")

    def _on_event(self, kind, item):
        if str(item.get("type") or "").lower() != "ring":
            return
        cam = item.get("camera") or item.get("device") or {}
        cam_id = cam.get("id") if isinstance(cam, dict) else (item.get("camera") if isinstance(item.get("camera"), str) else None)
        cam_id = cam_id or item.get("cameraId")
        door = self._by_camera.get(cam_id)
        if not door and len(self.doorbells) == 1 and not self.doorbells[0]["camera_id"]:
            door = self.doorbells[0]           # one doorbell, unbound: any ring is it
        if not door:
            return
        now = time.time()
        if now - self._recent.get(cam_id, 0) < 3:
            return                              # a press bounces; one call per press
        self._recent[cam_id] = now
        self._ring(door, cam_id)

    def _ring(self, door, cam_id):
        with self._lock:
            if self._active and self._active.state != "done":
                self.log(f"protect: {door['name']} pressed while a call is up; ignoring")
                return
            # A discovery apply may have rebuilt the doors; make sure ours is present.
            if door["user"] not in self.bridge.doors:
                self._register_doors()
            call = ProtectCall(self, door, cam_id or door.get("camera_id"), self.log)
            self._active = call
        threading.Thread(target=call.run, name="protect-call", daemon=True).start()

    def _call_over(self, call):
        with self._lock:
            if self._active is call:
                self._active = None

    def ring_now(self, name_or_user):
        """Manual trigger (API /protect/ring), for testing without a press."""
        key = slugify(name_or_user)
        for d in self.doorbells:
            if d["user"] == key or slugify(d["name"]) == key:
                self._ring(d, d.get("camera_id"))
                return True
        return False

    def public(self):
        return {"enabled": self.enabled, "host": self.host,
                "ffmpeg": bool(shutil.which("ffmpeg")),
                "doorbells": [{"name": d["name"], "user": d["user"], "camera_id": d["camera_id"],
                               "ring": d["ring"], "talkback": d["talkback"]} for d in self.doorbells],
                "active": (self._active.public() if False else (self._active.state if self._active else None))}


def _cseq(msg):
    raw = (msg.get("cseq") or "").split()
    try:
        return int(raw[0]), (raw[1].upper() if len(raw) > 1 else "")
    except (ValueError, IndexError):
        return 0, ""
