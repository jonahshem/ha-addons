"""SIP, as much of it as a doorbell needs.

The protocol only - parsing, building, digest auth, SDP. No sockets, no
threads, no state. Everything here is a pure function over bytes or dicts,
which is the whole reason it can be tested without a phone system in the
room.

Why this exists at all: a browser cannot open a UDP socket, so it cannot be
a SIP endpoint. An intercom on the wall speaks SIP over UDP and nothing
else. This backend is the only thing that can see both, so it is the one
that has to speak SIP - and then hand the call to the panels over a
transport a browser does have.

Standard library only. RFC 3261 for the signalling, RFC 4566 for the SDP,
RFC 2617 for the digest - implemented to the extent that real intercoms and
PBXes actually use them, which is less than the RFCs describe and is called
out where it matters.
"""
import hashlib
import re
import secrets
import time

MAX_MESSAGE = 65535            # a UDP datagram, which is the whole message

# Compact header forms (RFC 3261 s7.3.3). An intercom that has to fit a
# packet in an MTU will use these, so a parser that only knows the long
# names silently fails to find the Call-ID of half the calls it sees.
COMPACT = {
    "i": "call-id", "m": "contact", "f": "from", "t": "to", "v": "via",
    "c": "content-type", "l": "content-length", "s": "subject",
    "k": "supported", "e": "content-encoding", "o": "event",
    "r": "refer-to", "b": "referred-by", "u": "allow-events",
    "x": "session-expires", "j": "reject-contact", "d": "request-disposition",
    "a": "accept-contact", "y": "identity",
}

# Headers that may appear more than once and whose order is meaningful.
# Via order *is* the return path, so flattening it breaks routing.
MULTI = {"via", "route", "record-route", "contact", "path"}

REQUEST_RE = re.compile(rb"^([A-Z]+) +(\S+) +SIP/2\.0$")
STATUS_RE = re.compile(rb"^SIP/2\.0 +(\d{3}) *(.*)$")


class Message:
    """One SIP message, in whichever direction.

    Headers are kept twice over: `headers` is the normalised lowercase map
    used for lookups, and `order` remembers how they arrived so a reply can
    be built that looks like the request it answers. Middleboxes are fussier
    about that than the RFC is.
    """

    def __init__(self, *, method=None, uri=None, status=None, reason=None,
                 headers=None, body=b""):
        self.method = method
        self.uri = uri
        self.status = status
        self.reason = reason
        self.headers = headers or {}
        self.body = body or b""

    # -- reading ----------------------------------------------------------
    @property
    def is_request(self):
        return self.method is not None

    def get(self, name, default=None):
        v = self.headers.get(name.lower())
        if v is None:
            return default
        return v[0] if isinstance(v, list) else v

    def all(self, name):
        v = self.headers.get(name.lower())
        if v is None:
            return []
        return list(v) if isinstance(v, list) else [v]

    @property
    def call_id(self):
        return self.get("call-id", "")

    @property
    def cseq(self):
        """(number, method) - the pair that says which transaction this is."""
        raw = (self.get("cseq") or "").split()
        try:
            return int(raw[0]), (raw[1].upper() if len(raw) > 1 else "")
        except (ValueError, IndexError):
            return 0, ""

    def param(self, header, name):
        """A ;name=value parameter off a header, e.g. the tag on From."""
        return params(self.get(header) or "").get(name.lower())

    def __repr__(self):
        what = f"{self.method} {self.uri}" if self.is_request else f"{self.status} {self.reason}"
        return f"<SIP {what} cseq={self.cseq[0]} {self.call_id[:12]}>"


def parse(raw):
    """Bytes off the wire to a Message, or None if it is not one.

    Deliberately forgiving: a malformed datagram from anywhere on the
    network should be dropped, not raise into the receive loop.
    """
    if not raw:
        return None
    head, _, body = raw.partition(b"\r\n\r\n")
    if not head:
        # Some devices send bare LF. Retry rather than reject.
        head, _, body = raw.partition(b"\n\n")
        if not head:
            return None
    lines = head.replace(b"\r\n", b"\n").split(b"\n")
    start = lines[0].strip()

    msg = Message()
    m = REQUEST_RE.match(start)
    if m:
        msg.method = m.group(1).decode("ascii", "replace").upper()
        msg.uri = m.group(2).decode("utf-8", "replace")
    else:
        m = STATUS_RE.match(start)
        if not m:
            return None
        msg.status = int(m.group(1))
        msg.reason = m.group(2).decode("utf-8", "replace").strip()

    # Unfold continuation lines before splitting on the colon: a long
    # header may legally arrive wrapped onto the next line.
    unfolded = []
    for line in lines[1:]:
        if line[:1] in (b" ", b"\t") and unfolded:
            unfolded[-1] += b" " + line.strip()
        else:
            unfolded.append(line)

    for line in unfolded:
        if not line.strip():
            continue
        name, sep, value = line.partition(b":")
        if not sep:
            continue
        key = name.strip().decode("ascii", "replace").lower()
        key = COMPACT.get(key, key)
        val = value.strip().decode("utf-8", "replace")
        if key in MULTI:
            msg.headers.setdefault(key, []).append(val)
        elif key in msg.headers:
            # A repeat of a single-value header: keep the first, which is
            # what a proxy would have used.
            continue
        else:
            msg.headers[key] = val

    # Trust Content-Length when it is there and sane, because a datagram can
    # carry trailing rubbish, but never let it read past what arrived.
    try:
        want = int(msg.get("content-length", ""))
        if 0 <= want <= len(body):
            body = body[:want]
    except (TypeError, ValueError):
        pass
    msg.body = body
    return msg


def build(msg):
    """A Message back to bytes, with Content-Length made honest."""
    body = msg.body or b""
    if msg.is_request:
        start = f"{msg.method} {msg.uri} SIP/2.0"
    else:
        start = f"SIP/2.0 {msg.status} {msg.reason or reason_for(msg.status)}"
    out = [start]
    for key, value in msg.headers.items():
        if key == "content-length":
            continue
        name = title_header(key)
        for one in (value if isinstance(value, list) else [value]):
            out.append(f"{name}: {one}")
    out.append(f"Content-Length: {len(body)}")
    return ("\r\n".join(out) + "\r\n\r\n").encode("utf-8") + body


def title_header(key):
    """Call-ID, not Call-Id. Some devices really do compare these literally."""
    special = {"call-id": "Call-ID", "cseq": "CSeq", "www-authenticate": "WWW-Authenticate",
               "min-expires": "Min-Expires", "max-forwards": "Max-Forwards",
               "user-agent": "User-Agent", "content-type": "Content-Type",
               "proxy-authenticate": "Proxy-Authenticate",
               "proxy-authorization": "Proxy-Authorization"}
    if key in special:
        return special[key]
    return "-".join(p.capitalize() for p in key.split("-"))


REASONS = {
    100: "Trying", 180: "Ringing", 183: "Session Progress", 200: "OK",
    202: "Accepted", 401: "Unauthorized", 403: "Forbidden", 404: "Not Found",
    405: "Method Not Allowed", 407: "Proxy Authentication Required",
    408: "Request Timeout", 415: "Unsupported Media Type",
    420: "Bad Extension", 423: "Interval Too Brief", 480: "Temporarily Unavailable",
    486: "Busy Here", 487: "Request Terminated", 488: "Not Acceptable Here",
    500: "Server Internal Error", 503: "Service Unavailable", 603: "Decline",
}


def reason_for(status):
    return REASONS.get(int(status or 0), "OK" if 200 <= (status or 0) < 300 else "Error")


# -- the small syntactic pieces -------------------------------------------

def params(value):
    """The ;key=value tail of a header, lowercased keys, quotes stripped."""
    out = {}
    depth = 0
    piece = ""
    pieces = []
    # Split on semicolons that are not inside <> or "": a URI can contain
    # its own parameters and they are not the header's.
    quoted = False
    for ch in value or "":
        if ch == '"':
            quoted = not quoted
        elif not quoted and ch == "<":
            depth += 1
        elif not quoted and ch == ">":
            depth = max(0, depth - 1)
        if ch == ";" and not quoted and depth == 0:
            pieces.append(piece)
            piece = ""
            continue
        piece += ch
    pieces.append(piece)
    for one in pieces[1:]:
        key, _, val = one.partition("=")
        out[key.strip().lower()] = val.strip().strip('"')
    return out


def uri_of(value):
    """The bare sip: URI out of a name-addr, e.g. `"Door" <sip:a@b>;tag=x`."""
    if not value:
        return ""
    m = re.search(r"<([^>]+)>", value)
    if m:
        return m.group(1)
    return value.split(";")[0].strip()


def user_of(uri):
    """The user part - who is calling, in the only form worth showing."""
    bare = uri_of(uri)
    bare = re.sub(r"^sips?:", "", bare)
    user = bare.split("@")[0]
    return user.split(";")[0].split(":")[0]


def host_of(uri):
    bare = re.sub(r"^sips?:", "", uri_of(uri))
    host = bare.split("@")[-1].split(";")[0]
    return host


def new_tag():
    return secrets.token_hex(6)


def new_call_id(host="homeui"):
    return f"{secrets.token_hex(10)}@{host}"


def new_branch():
    # z9hG4bK is the magic cookie that says "this branch is RFC 3261".
    return "z9hG4bK" + secrets.token_hex(8)


# -- digest authentication -------------------------------------------------

def auth_params(challenge):
    """The comma-separated parameters of an auth header.

    Not the same shape as a header's ;parameters, and the difference bites:
    `qop="auth,auth-int"` has a comma *inside* the quotes. Split on that
    comma and qop comes out as "auth" plus a stray, the credential is built
    without qop, and the server rejects it - which looks exactly like a
    wrong password.
    """
    text = re.sub(r"^\s*\w+\s+", "", challenge or "")
    out = {}
    quoted = False
    piece = ""
    pieces = []
    for ch in text:
        if ch == '"':
            quoted = not quoted
        if ch == "," and not quoted:
            pieces.append(piece)
            piece = ""
            continue
        piece += ch
    pieces.append(piece)
    for one in pieces:
        key, sep, val = one.partition("=")
        if not sep:
            continue
        out[key.strip().lower()] = val.strip().strip('"')
    return out


def digest(challenge, *, username, password, method, uri, cnonce=None, nc=1):
    """Answer a WWW-Authenticate / Proxy-Authenticate challenge.

    MD5 only, with and without qop. Not because the alternatives are not
    worth having, but because an intercom that offers SHA-256 does not
    exist yet in this trade, and an unused branch is an untested one. If one
    turns up, `algorithm` is where it would go.
    """
    ch = auth_params(challenge)
    realm = ch.get("realm", "")
    nonce = ch.get("nonce", "")
    opaque = ch.get("opaque")
    qop_offered = [q.strip() for q in (ch.get("qop") or "").split(",") if q.strip()]

    def md5(s):
        return hashlib.md5(s.encode("utf-8")).hexdigest()

    ha1 = md5(f"{username}:{realm}:{password}")
    ha2 = md5(f"{method}:{uri}")

    fields = {
        "username": username, "realm": realm, "nonce": nonce, "uri": uri,
        "algorithm": "MD5",
    }
    if "auth" in qop_offered:
        cnonce = cnonce or secrets.token_hex(8)
        nc_s = f"{nc:08x}"
        response = md5(f"{ha1}:{nonce}:{nc_s}:{cnonce}:auth:{ha2}")
        fields.update({"response": response, "qop": "auth", "nc": nc_s, "cnonce": cnonce})
    else:
        fields["response"] = md5(f"{ha1}:{nonce}:{ha2}")
    if opaque:
        fields["opaque"] = opaque

    # nc and algorithm are the two that must not be quoted, or servers
    # reject the whole credential.
    bare = {"nc", "algorithm", "qop"}
    parts = [f'{k}={v}' if k in bare else f'{k}="{v}"' for k, v in fields.items()]
    return "Digest " + ", ".join(parts)


# -- SDP -------------------------------------------------------------------

PCMU = 0
# G.722 is 16 kHz audio, but RFC 3551 froze its RTP clock at 8000 by mistake
# and everyone kept the mistake. The rtpmap MUST say 8000 or the far end
# plays it at the wrong speed.
G722 = 9
PCMA = 8
TELEPHONE_EVENT = 101

CODEC_NAMES = {PCMU: "PCMU/8000", PCMA: "PCMA/8000", G722: "G722/8000"}


def rtpmap_for(pt):
    """The rtpmap a payload type must be announced with."""
    return CODEC_NAMES.get(pt, "PCMU/8000")


H264 = 96          # the usual dynamic type; the far end's own number is echoed back


def sdp_answer(*, address, audio_port, audio_codec=PCMU, video_port=None, video=None,
               session_id=None, audio_direction="sendrecv"):
    """The answer to a door station's offer.

    Audio is G.711 because everything speaks it. Video, when the far end
    offered any, is answered `recvonly` and with **their** payload type and
    their `fmtp` echoed back - a camera picks its own dynamic number and its
    own profile, and an answer that renames either is an answer it will not
    accept.

    Nothing is offered that this bridge cannot carry: no video is answered if
    the offer had none, and it never claims to send video, only to receive it.
    """
    sid = session_id or int(time.time())
    lines = [
        "v=0",
        f"o=homeui {sid} {sid} IN IP4 {address}",
        "s=HomeUI",
        f"c=IN IP4 {address}",
        "t=0 0",
        f"m=audio {audio_port} RTP/AVP {audio_codec} {TELEPHONE_EVENT}",
        f"a=rtpmap:{audio_codec} {rtpmap_for(audio_codec)}",
        f"a=rtpmap:{TELEPHONE_EVENT} telephone-event/8000",
        f"a=fmtp:{TELEPHONE_EVENT} 0-15",
        "a=ptime:20",
        # `recvonly` during early media: the door's picture is worth showing
        # while it rings, but the house must not be audible at the front door
        # before anybody has answered.
        f"a={audio_direction}",
    ]

    if video and video_port:
        pt = video.get("payload", H264)
        lines += [
            f"m=video {video_port} RTP/AVP {pt}",
            f"a=rtpmap:{pt} {video.get('rtpmap') or 'H264/90000'}",
        ]
        if video.get("fmtp"):
            lines.append(f"a=fmtp:{pt} {video['fmtp']}")
        # This bridge has no camera of its own and never will: it receives the
        # door's picture and shows it. Saying sendrecv would invite a stream
        # nobody is listening for.
        lines.append("a=recvonly")

    return ("\r\n".join(lines) + "\r\n").encode("ascii")


# The old name, kept so nothing that only wants audio has to change.
def sdp_offer(*, address, port, session_id=None, codecs=(PCMU, PCMA)):
    """An audio-only answer. Superseded by `sdp_answer`."""
    return sdp_answer(address=address, audio_port=port, audio_codec=codecs[0],
                      session_id=session_id)


def sdp_parse(body):
    """Both media sections of an offer: where to send, and in what.

    Returns `{"audio": {...} | None, "video": {...} | None}`. A door station
    offers video on its own `m=` line with its own port, its own dynamic
    payload type and an `fmtp` carrying the profile - all three have to survive
    into the answer, so all three are kept here.
    """
    out = {"audio": None, "video": None}
    if not body:
        return out

    text = body.decode("utf-8", "replace")
    session_addr = None
    current = None

    def start(kind, parts):
        try:
            port = int(parts[1])
        except (IndexError, ValueError):
            port = None
        return {"kind": kind, "address": session_addr, "port": port,
                "payloads": [int(p) for p in parts[3:] if p.isdigit()],
                "rtpmaps": {}, "fmtps": {}, "direction": "sendrecv"}

    for line in text.replace("\r\n", "\n").split("\n"):
        line = line.strip()
        if line.startswith("c=IN IP4 "):
            addr = line[9:].split("/")[0].strip()
            if current is None:
                session_addr = addr
            else:
                current["address"] = addr
        elif line.startswith("m="):
            parts = line[2:].split()
            kind = parts[0] if parts else ""
            current = start(kind, parts) if kind in ("audio", "video") else None
            if current is not None:
                out[kind] = current
        elif current is None:
            continue
        elif line.startswith("a=rtpmap:"):
            num, _, name = line[9:].partition(" ")
            if num.isdigit():
                current["rtpmaps"][int(num)] = name.strip()
        elif line.startswith("a=fmtp:"):
            num, _, params = line[7:].partition(" ")
            if num.isdigit():
                current["fmtps"][int(num)] = params.strip()
        elif line[2:] in ("sendonly", "recvonly", "sendrecv", "inactive"):
            current["direction"] = line[2:]

    for media in out.values():
        if media is not None and media.get("port") is None:
            out[media["kind"]] = None
    return out


def pick_video(offer):
    """The H.264 stream in an offer, if there is one this bridge can carry.

    H.264 only. A door station that offers H.265 or VP8 is not refused the
    call - it simply gets audio, because a picture nobody can decode is worth
    less than a working intercom.
    """
    video = (offer or {}).get("video")
    if not video or not video.get("port"):
        return None
    for pt in video["payloads"]:
        name = (video["rtpmaps"].get(pt) or "").split("/")[0].strip().lower()
        # A dynamic type with no rtpmap is unidentifiable; 96 is the
        # conventional H.264 number but conventions are not promises.
        if name == "h264":
            return {"payload": pt, "rtpmap": video["rtpmaps"].get(pt),
                    "fmtp": video["fmtps"].get(pt), "address": video.get("address"),
                    "port": video["port"]}
    return None


def pick_codec(offer, ours=(G722, PCMU, PCMA)):
    """The caller's most-preferred codec that we also have.

    Their order, not ours: RFC 3264 says the offer is listed in preference
    order and an answerer that overrides it for no reason is the one who
    caused the problem. Both halves of G.711 are 8-bit 8 kHz and decode the
    same way here, so there is nothing to fight over.
    """
    audio = (offer or {}).get("audio") or {}
    for p in audio.get("payloads") or []:
        if p in ours:
            return p
    return None
