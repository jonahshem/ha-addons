"""One UniFi Protect console's Integration API, the parts a door needs.

A Protect doorbell is not a SIP device. It has no extension, it does not
register anywhere, and its media lives behind Ubiquiti's own API: an RTSP pull
for the picture and the visitor's voice, and a `talkback-session` for talking
back out of its speaker. This module speaks that API so `protectdoor.py` can
turn a press into a call the Rava bridge already knows how to ring.

    GET  /v1/cameras                         which doorbells exist
    GET/POST /v1/cameras/{id}/rtsps-stream   the RTSP URL (enable if off)
    POST /v1/cameras/{id}/talkback-session   where to send the reply audio
    WS   /v1/subscribe/events                the press itself ("ring")

Auth is the `X-API-KEY` header (Protect app -> Settings -> Control Plane ->
Integrations). The console's certificate is self-signed and this is a LAN call
to an IP, so verification is off deliberately.

Adapted from UniFiCameraBridge/protect.py; standard library only.
"""
import base64
import hashlib
import json
import os
import socket
import ssl
import struct
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

QUALITIES = ("high", "medium", "low", "package")
MAIN_QUALITIES = ("high", "medium", "low")
RTSPS_PORT = 7441
RTSP_PORT = 7447
DEFAULT_TIMEOUT = 10


class ProtectError(Exception):
    """An API call failed; the message is meant to be read by an installer."""


def _ssl_context():
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    return ctx


class ProtectClient:
    """Talks to one UniFi Protect console's Integration API."""

    def __init__(self, host, api_key, timeout=DEFAULT_TIMEOUT):
        self.host = str(host or "").strip()
        self.api_key = str(api_key or "").strip()
        self.timeout = timeout
        self._opener = urllib.request.build_opener(
            urllib.request.HTTPSHandler(context=_ssl_context()))

    def _url(self, path):
        base = self.host
        if "://" not in base:
            base = "https://" + base
        return base.rstrip("/") + "/proxy/protect/integration" + path

    def _request(self, method, path, body=None, accept="application/json"):
        url = self._url(path)
        data = None
        headers = {"X-API-KEY": self.api_key, "Accept": accept}
        if body is not None:
            data = json.dumps(body).encode("utf-8")
            headers["Content-Type"] = "application/json"
        req = urllib.request.Request(url, data=data, headers=headers, method=method)
        try:
            with self._opener.open(req, timeout=self.timeout) as resp:
                return resp.status, resp.read(), resp.headers.get("Content-Type", "")
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", "replace")[:300]
            if exc.code in (401, 403):
                raise ProtectError(f"{method} {path} refused (HTTP {exc.code}): the API key is "
                                   f"wrong or from another console.") from exc
            raise ProtectError(f"{method} {path} -> HTTP {exc.code}: {detail}") from exc
        except urllib.error.URLError as exc:
            raise ProtectError(f"cannot reach the Protect console at {self.host}: {exc.reason}") from exc

    def _json(self, method, path, body=None):
        _s, payload, _c = self._request(method, path, body=body)
        if not payload:
            return None
        try:
            return json.loads(payload.decode("utf-8"))
        except ValueError as exc:
            raise ProtectError(f"{method} {path} did not return JSON: {exc}") from exc

    def meta(self):
        return self._json("GET", "/v1/meta/info")

    def cameras(self):
        data = self._json("GET", "/v1/cameras")
        if not isinstance(data, list):
            raise ProtectError("GET /v1/cameras did not return a list")
        return data

    def rtsps_streams(self, camera_id):
        data = self._json("GET", "/v1/cameras/%s/rtsps-stream" % urllib.parse.quote(camera_id))
        return {q: (data or {}).get(q) for q in QUALITIES}

    def create_rtsps_streams(self, camera_id, qualities):
        data = self._json("POST", "/v1/cameras/%s/rtsps-stream" % urllib.parse.quote(camera_id),
                          body={"qualities": list(qualities)})
        return {q: (data or {}).get(q) for q in QUALITIES}

    def talkback_session(self, camera_id):
        """Where to send the reply audio, and in what format.

        Returns `{url: "rtp://ip:port", codec, samplingRate, bitsPerSample}`.
        The URL is a plain RTP sink on the camera itself; whoever POSTs gets a
        fresh one, so this is called once per answered call.
        """
        return self._json("POST", "/v1/cameras/%s/talkback-session" % urllib.parse.quote(camera_id))

    # -- picking the doorbell's stream -------------------------------------

    def stream_url(self, camera_id, quality="high", enable=True):
        """A PLAY-able RTSP URL for one camera, turning RTSP on if it is off.

        Plain rtsp:// (port 7447) rather than rtsps:// (7441): ffmpeg then does
        not have to negotiate TLS with a self-signed console and SRTP on top,
        and this is a LAN pull to an IP. Falls back to the next quality if the
        asked-for one has no stream.
        """
        order = [quality] + [q for q in MAIN_QUALITIES if q != quality]
        existing = self.rtsps_streams(camera_id)
        for q in order:
            if existing.get(q):
                return plain_rtsp_url(self.host, existing[q]), q
        if not enable:
            return None, None
        created = self.create_rtsps_streams(camera_id, [order[0]])
        for q in order:
            if created.get(q):
                return plain_rtsp_url(self.host, created[q]), q
        return None, None


# -- URL shaping ------------------------------------------------------------

def alias_from_rtsps_url(url):
    if not url:
        return None
    return urllib.parse.urlparse(url).path.lstrip("/") or None


def plain_rtsp_url(host, rtsps_url, port=RTSP_PORT):
    alias = alias_from_rtsps_url(rtsps_url)
    if not alias:
        return None
    hostname = urllib.parse.urlparse(rtsps_url).hostname or (host.split("://")[-1])
    return "rtsp://%s:%d/%s" % (hostname, port, alias)


# -- the events WebSocket ---------------------------------------------------

class EventsSocket:
    """Holds `/v1/subscribe/events` open and calls back on each event.

    A minimal RFC 6455 client: TLS, the upgrade handshake with `X-API-KEY`,
    and masked text/binary frames in, control frames answered. Reconnects with
    backoff, because a doorbell that stops ringing after a Wi-Fi blip is a
    doorbell that gets replaced.
    """

    def __init__(self, host, api_key, on_event, log=print):
        self.host = str(host or "").strip()
        self.api_key = str(api_key or "").strip()
        self.on_event = on_event
        self.log = log
        self._stop = threading.Event()
        self._sock = None

    def start(self):
        threading.Thread(target=self._run, name="protect-events", daemon=True).start()

    def stop(self):
        self._stop.set()
        try:
            if self._sock:
                self._sock.close()
        except OSError:
            pass

    def _hostport(self):
        netloc = self.host.split("://")[-1].rstrip("/")
        host, _, port = netloc.partition(":")
        return host, int(port) if port else 443

    def _run(self):
        backoff = 1
        while not self._stop.is_set():
            try:
                self._connect_and_read()
                backoff = 1
            except Exception as e:
                if not self._stop.is_set():
                    self.log(f"protect events: {e!r}; retrying in {backoff}s")
                    self._stop.wait(backoff)
                    backoff = min(backoff * 2, 30)

    def _connect_and_read(self):
        host, port = self._hostport()
        raw = socket.create_connection((host, port), timeout=DEFAULT_TIMEOUT)
        sock = _ssl_context().wrap_socket(raw, server_hostname=host)
        self._sock = sock
        key = base64.b64encode(os.urandom(16)).decode("ascii")
        path = "/proxy/protect/integration/v1/subscribe/events"
        req = (f"GET {path} HTTP/1.1\r\nHost: {host}:{port}\r\n"
               "Upgrade: websocket\r\nConnection: Upgrade\r\n"
               f"Sec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n"
               f"X-API-KEY: {self.api_key}\r\n\r\n")
        sock.sendall(req.encode("ascii"))
        buf = self._read_until(sock, b"\r\n\r\n")
        head = buf.decode("latin1")
        if " 101 " not in head.split("\r\n", 1)[0]:
            raise ProtectError("events socket refused the upgrade: " + head.split("\r\n", 1)[0])
        accept = base64.b64encode(hashlib.sha1(
            (key + "258EAFA5-E914-47DA-95CA-C5AB0DC85B11").encode()).digest()).decode()
        if accept.lower() not in head.lower():
            raise ProtectError("events socket gave a bad accept key")
        self.log("protect events: subscribed")
        rest = buf.split(b"\r\n\r\n", 1)[1]
        self._read_frames(sock, rest)

    @staticmethod
    def _read_until(sock, marker):
        buf = b""
        while marker not in buf:
            chunk = sock.recv(4096)
            if not chunk:
                raise ProtectError("events socket closed during handshake")
            buf += chunk
            if len(buf) > 65536:
                raise ProtectError("events handshake header too large")
        return buf

    def _read_frames(self, sock, initial=b""):
        buf = bytearray(initial)
        while not self._stop.is_set():
            frame, buf = self._take_frame(sock, buf)
            if frame is None:
                continue
            opcode, payload = frame
            if opcode == 0x8:            # close
                return
            if opcode == 0x9:            # ping -> pong
                sock.sendall(self._frame(0xA, payload))
                continue
            if opcode in (0x1, 0x2):     # text / binary
                self._dispatch(payload)

    def _take_frame(self, sock, buf):
        while len(buf) < 2:
            buf += self._recv(sock)
        b0, b1 = buf[0], buf[1]
        opcode = b0 & 0x0F
        masked = b1 & 0x80
        ln = b1 & 0x7F
        idx = 2
        if ln == 126:
            while len(buf) < 4:
                buf += self._recv(sock)
            ln = struct.unpack(">H", buf[2:4])[0]
            idx = 4
        elif ln == 127:
            while len(buf) < 10:
                buf += self._recv(sock)
            ln = struct.unpack(">Q", buf[2:10])[0]
            idx = 10
        mask = b""
        if masked:
            while len(buf) < idx + 4:
                buf += self._recv(sock)
            mask = buf[idx:idx + 4]
            idx += 4
        while len(buf) < idx + ln:
            buf += self._recv(sock)
        payload = bytes(buf[idx:idx + ln])
        if masked and mask:
            payload = bytes(payload[i] ^ mask[i % 4] for i in range(len(payload)))
        return (opcode, payload), bytearray(buf[idx + ln:])

    def _recv(self, sock):
        chunk = sock.recv(8192)
        if not chunk:
            raise ProtectError("events socket closed")
        return chunk

    @staticmethod
    def _frame(opcode, payload=b""):
        # Client frames must be masked (RFC 6455 s5.3).
        mask = os.urandom(4)
        masked = bytes(payload[i] ^ mask[i % 4] for i in range(len(payload)))
        ln = len(payload)
        if ln < 126:
            head = struct.pack("!BB", 0x80 | opcode, 0x80 | ln)
        elif ln < 65536:
            head = struct.pack("!BBH", 0x80 | opcode, 0x80 | 126, ln)
        else:
            head = struct.pack("!BBQ", 0x80 | opcode, 0x80 | 127, ln)
        return head + mask + masked

    def _dispatch(self, payload):
        try:
            msg = json.loads(payload.decode("utf-8", "replace"))
        except ValueError:
            return
        # An eventAdd/eventUpdate: {"type":"add","item":{...}} - the ring is in item.
        item = msg.get("item") if isinstance(msg, dict) else None
        if isinstance(item, dict):
            try:
                self.on_event(msg.get("type"), item)
            except Exception as e:
                self.log(f"protect events: handler error: {e!r}")
