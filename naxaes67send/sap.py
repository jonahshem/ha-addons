"""SAP/SDP announcer (RFC 2974) so the DM NAX autodiscovers our AES67 stream.

Unchanged in substance from the version proven at 14 Malke on 2026-09-03. The
NAX only listens for SAP; nothing on that network was announcing, which is why
the stream was invisible until this existed.
"""
import json
import os
import random
import socket
import struct
import threading
import time

# The NAX's own PTP grandmaster, read from NaxSdp TsRefClkValue on the live
# device. Overridable because it is per-amplifier: a second house will have a
# different one and pointing at this one would announce a clock that is not
# there.
GM = "00-10-7F-FF-FE-F4-2F-E0"


def build_sdp(src_ip, mcast, port, session_name, ch=2, rate=48000, gm=GM, sid=None, version=None):
    sid = int(time.time()) if sid is None else sid
    version = sid if version is None else version
    return "\r\n".join([
        "v=0",
        f"o=- {sid} {version} IN IP4 {src_ip}",
        f"s={session_name}",
        f"c=IN IP4 {mcast}/32",
        "t=0 0",
        "a=clock-domain:PTPv2 0",
        f"m=audio {port} RTP/AVP 98",
        f"c=IN IP4 {mcast}/32",
        f"a=rtpmap:98 L24/{rate}/{ch}",
        "a=sync-time:0",
        "a=framecount:48",
        "a=ptime:1",
        "a=mediaclk:direct=0",
        f"a=ts-refclk:ptp=IEEE1588-2008:{gm}:0",
        "a=recvonly",
        "",
    ])


def eui64(clock_id):
    """A PTP clock id (uint64) as the SDP writes it: 8 hex pairs, dash-joined."""
    return "-".join(f"{(int(clock_id) >> (8 * (7 - i))) & 0xFF:02X}" for i in range(8))


def build_sap(src_ip, sdp, msg_hash, deletion=False):
    # V=1, A=0 (IPv4), R=0, T=0 (announce) / 1 (delete), E=0, C=0 -> 0x20 / 0x24; auth_len = 0
    flags = 0x24 if deletion else 0x20
    hdr = struct.pack("!BBH", flags, 0x00, msg_hash) + socket.inet_aton(src_ip)
    return hdr + b"application/sdp\x00" + sdp.encode("utf-8")


# Where the session's identity is kept between runs (the add-on's /data survives
# restarts and updates). See Announcement.
STATE_FILE = os.environ.get("SAP_STATE_FILE", "/data/sap-session.json")


class Announcement:
    """The SAP packet for our session, rebuilt when the grandmaster changes.

    🔴 `gm` may be a callable returning the grandmaster now. Until 0.15.5 it
    was read once at start-up and announced for the life of the process. At
    110 Roosevelt everything restarted together on 2026-11-06; while the
    amplifiers came up, one of them (NAX-5, .55) was the grandmaster when the
    add-on read it, and for five days it advertised that clock while all six
    amplifiers had settled on NAX-1 (.51). The doorbell played only some of
    the time. A restart fixed it; this stops it needing one.

    A changed announcement keeps the session id and takes a new version in
    `o=` and a new SAP message hash (RFC 2974: a changed announcement must
    change its hash), so a receiver sees the same session, updated.

    🔴 And the session id survives a RESTART (`state_file`). Until 0.15.5 every
    start announced a brand-new session id under the same name. At 110 the
    DM-NAX-8ZSAs swapped to the new one after the 2026-11-11 restart, but the
    three DM-NAX-4ZSA-50s (fw 3.1.0139) kept the old one - with the stale
    clock - and stayed silent for every page until they were rebooted. Now a
    restart is the same session at a higher version, which is how SAP says
    "this replaces what you have".
    """

    def __init__(self, src_ip, mcast, port, session_name, gm=GM, state_file=None):
        self.src_ip, self.mcast, self.port, self.session_name = src_ip, mcast, port, session_name
        self._gm = gm
        self.state_file = state_file
        self.sid = int(time.time())
        self.version = self.sid
        saved = self._load()
        if saved:
            self.sid = saved["sid"]
            self.version = max(int(saved.get("version", self.sid)) + 1, int(time.time()))
        self.gm = None
        self.sdp = self.pkt = None
        self.msg_hash = None

    def _identity(self):
        return {"session": self.session_name, "mcast": self.mcast, "port": int(self.port),
                "src": self.src_ip}

    def _load(self):
        """The saved session, if it is this same stream (name, group, port,
        source). Anything else - a renamed session, another group - is a new
        session and rightly gets a new id."""
        if not self.state_file:
            return None
        try:
            with open(self.state_file, encoding="utf-8") as f:
                st = json.load(f)
            if st.get("identity") == self._identity() and int(st.get("sid", 0)) > 0:
                return st
        except (OSError, ValueError, TypeError):
            pass
        return None

    def _save(self):
        if not self.state_file:
            return
        try:
            tmp = self.state_file + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump({"identity": self._identity(), "sid": self.sid, "version": self.version,
                           "gm": self.gm}, f)
            os.replace(tmp, self.state_file)
        except OSError:
            pass   # a read-only /data only costs the restart behaviour, never the stream

    def deletion(self):
        """The SAP deletion for what we are announcing now, or None."""
        if self.sdp is None:
            return None
        return build_sap(self.src_ip, self.sdp, self.msg_hash, deletion=True)

    def _read_gm(self):
        if not callable(self._gm):
            return self._gm
        try:
            got = self._gm()
        except Exception:
            got = None
        # A failed or empty read (PTP re-electing) keeps what we announce.
        return got or self.gm or GM

    def current(self):
        """(packet, changed_from): changed_from is the previous grandmaster
        when it has just changed, else None (also on the very first build)."""
        gm = self._read_gm()
        if self.pkt is not None and gm == self.gm:
            return self.pkt, None
        old = self.gm
        if self.pkt is not None:
            self.version = max(self.version + 1, int(time.time()))
        self.gm = gm
        self.sdp = build_sdp(self.src_ip, self.mcast, self.port, self.session_name, gm=gm,
                             sid=self.sid, version=self.version)
        self.msg_hash = random.randint(1, 0xFFFF)
        self.pkt = build_sap(self.src_ip, self.sdp, self.msg_hash)
        self._save()
        return self.pkt, old


# Every running announcer: what `/health` and the amplifier check compare
# against, and what `stop_all` withdraws on shutdown.
_RUNNING = []
_RUN_LOCK = threading.Lock()


def running():
    """[{session, sid, version, gm}] of what this process is announcing now."""
    with _RUN_LOCK:
        return [{"session": a.session_name, "sid": a.sid, "version": a.version, "gm": a.gm}
                for a, _, _ in _RUNNING]


def stop_all(log=print):
    """Send a SAP deletion for every announcement, so an amplifier drops it
    rather than holding it after we have gone. Called on SIGTERM (an add-on
    stop, restart or update). Best effort: two copies, no waiting."""
    with _RUN_LOCK:
        items = list(_RUNNING)
    for ann, s, targets in items:
        pkt = ann.deletion()
        if pkt is None:
            continue
        for _ in range(2):
            for dst in targets:
                try:
                    s.sendto(pkt, dst)
                except Exception:
                    pass
        log(f"[sap] withdrew {ann.session_name!r} (session {ann.sid} version {ann.version})")


def announce_forever(src_ip, mcast, port, session_name, iface_ip=None, gm=GM,
                     targets=(("239.255.255.255", 9875), ("224.2.127.254", 9875)),
                     interval=10.0, log=print, state_file=None):
    ann = Announcement(src_ip, mcast, port, session_name, gm=gm, state_file=state_file)
    pkt, _ = ann.current()
    sdp = ann.sdp
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 16)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
    if iface_ip:
        s.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_IF, socket.inet_aton(iface_ip))
    with _RUN_LOCK:
        _RUNNING.append((ann, s, targets))
    log(f"[sap] announcing {session_name!r} {mcast}:{port} -> {targets} every {interval}s"
        f" (session {ann.sid} version {ann.version})")
    log("[sap] SDP:\n" + sdp)
    n = 0
    while True:
        pkt, was = ann.current()
        if was is not None:
            log(f"[sap] PTP grandmaster changed {was} -> {ann.gm}; announcing version {ann.version}")
            log("[sap] SDP:\n" + ann.sdp)
        for dst in targets:
            try:
                s.sendto(pkt, dst)
            except Exception as e:
                log(f"[sap] send {dst} failed: {e}")
        n += 1
        if n <= 3 or n % 30 == 0:
            log(f"[sap] announcement #{n} sent")
        time.sleep(interval)


def start(src_ip, mcast, port, session_name, iface_ip=None, gm=GM, log=print,
          state_file=STATE_FILE):
    t = threading.Thread(target=announce_forever, daemon=True,
                         kwargs=dict(src_ip=src_ip, mcast=mcast, port=port,
                                     session_name=session_name,
                                     iface_ip=iface_ip, gm=gm, log=log, state_file=state_file))
    t.start()
    return t
