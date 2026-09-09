"""A Crestron page, turned into something the NAX stream can play.

A page arrives as G.711 u-law RTP on the panels' multicast group: 8 kHz, mono,
160 samples (20 ms) per packet, payload type 0. The AES67 sender wants the
opposite of that in every dimension - S24BE, 48 kHz, stereo - so this converts,
and streams the result to the sender while the page is still being spoken.

    page RTP (u-law 8k mono)  ->  PCM16  ->  48k stereo S24BE  ->  sender /live

Why per-packet conversion lines up exactly: one 20 ms packet is 160 samples,
which at 48 kHz is 960 frames, which as stereo S24BE is 5760 bytes - and the
sender pushes the stream in 288-byte millisecond frames, so 5760 is exactly 20
of them. Every chunk we send is frame-aligned, which matters because the sender
zero-pads a partial frame and a zero-pad in the middle of speech is a click.

Standard library only.
"""
import http.client
import threading
import urllib.parse

# -- G.711 u-law ------------------------------------------------------------
# Decoded once into a table: 256 entries, so the hot path is a lookup.
_BIAS = 0x84


def _ulaw_to_linear(u):
    u = ~u & 0xFF
    t = ((u & 0x0F) << 3) + _BIAS
    t <<= (u & 0x70) >> 4
    t -= _BIAS
    return -t if (u & 0x80) else t


ULAW = [_ulaw_to_linear(i) for i in range(256)]

RATE_IN, RATE_OUT = 8000, 48000
UP = RATE_OUT // RATE_IN          # 6
CH_OUT = 2
WIDTH_OUT = 3                     # S24BE


def ulaw_to_s24be(payload, last=[0]):
    """One u-law packet -> 48 kHz stereo S24BE bytes.

    Upsampled by linear interpolation rather than sample-repeat: a page is
    speech, and holding each sample for six output frames puts a buzz on top
    of it that is obvious on a real speaker. `last` carries the previous
    packet's final sample so the interpolation is continuous across packets
    instead of stepping to it from zero every 20 ms.
    """
    out = bytearray()
    prev = last[0]
    for b in payload:
        cur = ULAW[b]
        # UP output samples spanning prev -> cur
        for k in range(1, UP + 1):
            s = prev + (cur - prev) * k // UP
            v = (s << 8) & 0xFFFFFF          # 16-bit into the top of 24, two's complement
            three = bytes((v >> 16 & 0xFF, v >> 8 & 0xFF, v & 0xFF))
            out += three * CH_OUT            # same sample to both channels
        prev = cur
    last[0] = prev
    return bytes(out)


class Ulaw48k:
    """Per-page converter, so two pages never share interpolation state."""

    def __init__(self):
        self._last = [0]

    def convert(self, payload):
        return ulaw_to_s24be(payload, self._last)


# -- streaming it to the AES67 sender ---------------------------------------

class PageRelay:
    """A chunked POST held open for the length of the page.

    Opened on the page's first packet and closed when the page goes quiet. The
    sender routes the zones when the request arrives and puts them back when
    the body ends, so simply closing the stream is what restores the house.
    """

    def __init__(self, url, token, zones, log=print):
        self.url = url.rstrip("/")
        self.token = token
        self.zones = list(zones or [])
        self.log = log
        self.conn = None
        self.lock = threading.Lock()
        self.sent = 0
        self.failed = None

    @property
    def open(self):
        return self.conn is not None

    def start(self):
        if self.conn is not None or not self.zones:
            return False
        parts = urllib.parse.urlsplit(self.url)
        path = (parts.path or "/").rstrip("/") + "/live?zones=" + urllib.parse.quote(",".join(self.zones))
        try:
            # The sender answers only after the page has been ROUTED, played and
            # drained: five zones bind one after another before the first
            # sound, and the tail runs three seconds past the last byte. Ten
            # seconds timed out on every five-zone page at 14 Malke - the page
            # still played, but the bridge logged no result and the sender hit
            # a broken pipe writing its 200 to a closed socket.
            conn = http.client.HTTPConnection(parts.hostname, parts.port or 80, timeout=90)
            conn.putrequest("POST", path)
            conn.putheader("Content-Type", "application/octet-stream")
            conn.putheader("Transfer-Encoding", "chunked")
            if self.token:
                conn.putheader("Authorization", f"Bearer {self.token}")
            conn.endheaders()
        except Exception as e:
            self.failed = repr(e)
            self.log(f"page relay: could not open the stream: {e!r}")
            return False
        self.conn = conn
        self.sent = 0
        self.log(f"page relay: streaming the page to {self.zones}")
        return True

    def push(self, pcm):
        with self.lock:
            if self.conn is None:
                return
            try:
                self.conn.send(b"%x\r\n" % len(pcm) + pcm + b"\r\n")
                self.sent += len(pcm)
            except Exception as e:
                self.failed = repr(e)
                self.log(f"page relay: stream broke after {self.sent} bytes: {e!r}")
                self._drop()

    def stop(self):
        with self.lock:
            if self.conn is None:
                return None
            conn, self.conn = self.conn, None
            try:
                conn.send(b"0\r\n\r\n")          # the end of a chunked body
                resp = conn.getresponse()
                body = resp.read(600).decode("utf-8", "replace")
                self.log(f"page relay: {self.sent} bytes sent, sender said {resp.status} {body[:200]}")
                return {"status": resp.status, "body": body, "bytes": self.sent}
            except Exception as e:
                self.log(f"page relay: closing the stream failed: {e!r}")
                return {"error": repr(e), "bytes": self.sent}
            finally:
                try:
                    conn.close()
                except Exception:
                    pass

    def _drop(self):
        try:
            self.conn.close()
        except Exception:
            pass
        self.conn = None
