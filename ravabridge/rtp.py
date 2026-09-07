"""RTP, only as far as carrying a voice.

Header packing and unpacking, and nothing else - no codec, no mixing, no
resampling. The audio is G.711, and G.711 is decoded in the browser where
the audio is going anyway. So this server never looks inside a payload: it
takes the bytes off one transport and puts them on the other.

That is worth being deliberate about. A stdlib Python process doing
per-sample arithmetic on 50 packets a second per call is the kind of thing
that works on a test bench and falls over on a panel host running four
houses. Moving bytes does not.

RFC 3550 for the header, RFC 4733 for the digits.
"""
import secrets
import struct

HEADER = struct.Struct("!BBHII")
VERSION = 2
MAX_SEQ = 0xFFFF
MAX_TS = 0xFFFFFFFF

PCMU = 0
PCMA = 8
TELEPHONE_EVENT = 101

# 20 ms of 8 kHz G.711 - one sample per byte, which is why these are equal.
SAMPLES_PER_PACKET = 160
PACKET_MS = 20


def unpack(data):
    """One packet to (payload_type, sequence, timestamp, marker, payload).

    None for anything that is not plausibly RTP. A media port receives
    scans, STUN, and the tail of the last call; none of it should reach the
    audio path.
    """
    if not data or len(data) < HEADER.size:
        return None
    b0, b1, seq, ts, ssrc = HEADER.unpack_from(data, 0)
    if (b0 >> 6) != VERSION:
        return None
    csrc = b0 & 0x0F
    extension = bool(b0 & 0x10)
    padding = bool(b0 & 0x20)
    offset = HEADER.size + csrc * 4
    if len(data) < offset:
        return None
    if extension:
        # A profile extension is length-prefixed in 32-bit words, after a
        # 16-bit id we have no use for.
        if len(data) < offset + 4:
            return None
        words = struct.unpack_from("!H", data, offset + 2)[0]
        offset += 4 + words * 4
        if len(data) < offset:
            return None
    payload = data[offset:]
    if padding and payload:
        # The last byte counts the padding, itself included.
        pad = payload[-1]
        if 0 < pad <= len(payload):
            payload = payload[:-pad]
    return {"pt": b1 & 0x7F, "marker": bool(b1 & 0x80), "seq": seq,
            "ts": ts, "ssrc": ssrc, "payload": payload}


def pack(*, pt, seq, ts, ssrc, payload, marker=False):
    b0 = VERSION << 6
    b1 = (pt & 0x7F) | (0x80 if marker else 0)
    return HEADER.pack(b0, b1, seq & MAX_SEQ, ts & MAX_TS, ssrc) + payload


class Sender:
    """The outgoing half of one call's audio.

    Sequence and timestamp advance per packet rather than per wall clock:
    the far end reconstructs timing from these, so a packet delayed on its
    way out of here must still say when it was meant to be played, not when
    it happened to be sent.
    """

    def __init__(self, *, pt=PCMU, ssrc=None):
        self.pt = pt
        self.ssrc = ssrc if ssrc is not None else secrets.randbits(32)
        # Both start random, per RFC 3550 s5.1 - a fixed start makes streams
        # from different calls indistinguishable to anything replaying them.
        self.seq = secrets.randbits(16)
        self.ts = secrets.randbits(32)
        self.first = True

    def next(self, payload, samples=SAMPLES_PER_PACKET):
        """Wrap one payload. The first packet is marked, as a talkspurt is."""
        data = pack(pt=self.pt, seq=self.seq, ts=self.ts, ssrc=self.ssrc,
                    payload=payload, marker=self.first)
        self.first = False
        self.seq = (self.seq + 1) & MAX_SEQ
        self.ts = (self.ts + samples) & MAX_TS
        return data


class Reorder:
    """Just enough jitter handling to not play packets backwards.

    A real jitter buffer belongs at the point of playback, which is the
    browser, and that is where the depth is. This only drops the two things
    the browser cannot fix after the fact: a packet from an older stream,
    and a duplicate.
    """

    WINDOW = 3000          # sequence distance beyond which we assume a restart

    def __init__(self):
        self.last = None
        self.ssrc = None

    def accept(self, packet):
        if packet is None:
            return False
        if self.ssrc is None or packet["ssrc"] != self.ssrc:
            # A new source is a new call leg - take it and reset.
            self.ssrc = packet["ssrc"]
            self.last = packet["seq"]
            return True
        seq = packet["seq"]
        if self.last is None:
            self.last = seq
            return True
        # Distance on a 16-bit ring: forwards is a small positive number.
        ahead = (seq - self.last) & MAX_SEQ
        if ahead == 0:
            return False                        # duplicate
        if ahead < self.WINDOW:
            self.last = seq
            return True
        behind = (self.last - seq) & MAX_SEQ
        if behind < self.WINDOW:
            return False                        # late; the browser has moved on
        self.last = seq                         # a long jump: treat as a restart
        return True


def dtmf_digit(payload):
    """The digit out of a telephone-event payload, or None.

    Worth having even though nothing dials here: 2N keypads and PBX menus
    send DTMF, and a gate that opens on a code sends it this way rather than
    as audio.
    """
    if not payload or len(payload) < 4:
        return None
    event = payload[0]
    end = bool(payload[1] & 0x80)
    table = "0123456789*#ABCD"
    if event >= len(table):
        return None
    return {"digit": table[event], "end": end,
            "volume": payload[1] & 0x3F,
            "duration": struct.unpack_from("!H", payload, 2)[0]}
