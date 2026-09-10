"""The AES67 stream the NAX listens to, carrying silence until it is spoken to.

v0.1.0 emitted a 440 Hz tone forever, which proved the path. This version
keeps the same stream up but feeds it from a queue: silence normally, an
announcement when one arrives.

**The stream must never stop.** From the session that solved this: if the
sender stops, the NAX drops the session from its discovered list and routing a
zone to `Aes67` will not take. So there is no "start the stream when somebody
speaks" - the stream is always running, and what changes is what is in it.

That is also why this uses `appsrc` rather than swapping `filesrc` in and out.
A pipeline that is rebuilt per announcement is a pipeline that is briefly not
there, and being briefly not there is the one thing that breaks the binding.

Audio is 2ch/48k/S24BE at 1 ms packet time, which is what the NAX expects
(Crestron KB 1001151) and what the SAP announcement claims.
"""
import fcntl
import os
import queue
import socket
import struct
import subprocess
import tempfile
import threading

import gi
gi.require_version("Gst", "1.0")
gi.require_version("GstNet", "1.0")
from gi.repository import Gst, GstNet, GLib      # noqa: E402

import sap                                        # noqa: E402

Gst.init(None)

# Blank means "work it out". `end0` is the Raspberry Pi's built-in NIC and was
# the default here for as long as this only ran on Pis; an Intel box calls the
# same port `eno1` or `enp1s0` and a VM calls it `ens18`, so a hardcoded default
# is a stream sent out of an interface that does not exist. Houses that already
# have `end0` saved in their options keep it - see `resolve_iface`.
IFACE = os.environ.get("IFACE", "").strip()
MCAST = os.environ.get("MCAST", "239.69.4.4")     # inside 239.8.0.0-239.128.255.255
PORT = int(os.environ.get("PORT", "5004"))
PT = int(os.environ.get("PT", "98"))
CH = 2
RATE = 48000
WIDTH = 3                                          # S24BE: three bytes a sample
SESSION = os.environ.get("SESSION", "HA Announce 1")

# One millisecond of audio, which is the packet time the NAX is told to expect.
FRAME_BYTES = (RATE // 1000) * CH * WIDTH
SILENCE = b"\x00" * FRAME_BYTES

_pending = queue.Queue()
_playing = threading.Event()


def local_ip(towards="8.8.8.8"):
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect((towards, 1))
        return s.getsockname()[0]
    finally:
        s.close()


def iface_for(ip):
    """The name of the interface holding `ip`, or None.

    SIOCGIFADDR rather than parsing `ip addr`: this image has iproute2, but a
    name is worth reading straight from the kernel rather than out of text that
    changes format between releases.
    """
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        for _, name in socket.if_nameindex():
            try:
                packed = fcntl.ioctl(sock.fileno(), 0x8915,          # SIOCGIFADDR
                                     struct.pack("256s", name[:15].encode()))
            except OSError:
                continue                                # no IPv4 on this one
            if socket.inet_ntoa(packed[20:24]) == ip:
                return name
    finally:
        sock.close()
    return None


def resolve_iface(src):
    """Which interface the stream leaves by.

    `src` was already chosen by the kernel, by routing towards the multicast
    group, so the interface holding it is the right one by construction - on a
    Pi (`end0`), an Intel box (`eno1`) or a VM (`ens18`), with nobody typing a
    name.

    A name that *was* typed wins, because a box with two NICs on the house LAN
    is a real thing and only a person knows which one the amplifier is on. But a
    name that does not exist here is reported and worked around rather than
    used: a house whose NIC was renamed by an OS update should keep playing, and
    failing silently is the one outcome nobody can diagnose.
    """
    present = {name for _, name in socket.if_nameindex()}
    if IFACE and IFACE in present:
        return IFACE
    found = iface_for(src)
    if IFACE:
        print(f"[sender] configured iface={IFACE!r} is not on this box "
              f"({', '.join(sorted(present))}) - using {found or 'the default route'}",
              flush=True)
    return found


def decode_to_pcm(path):
    """Any file GStreamer can read -> raw S24BE/48k/2ch.

    Uses gst-launch rather than a library call because the add-on already
    ships the tools, and a separate process cannot wedge the live pipeline if
    somebody uploads something strange.
    """
    out = tempfile.NamedTemporaryFile(suffix=".raw", delete=False)
    out.close()
    cmd = ["gst-launch-1.0", "-q",
           "filesrc", f"location={path}", "!", "decodebin", "!",
           "audioconvert", "!", "audioresample", "!",
           f"audio/x-raw,format=S24BE,rate={RATE},channels={CH},layout=interleaved", "!",
           "filesink", f"location={out.name}"]
    r = subprocess.run(cmd, capture_output=True, timeout=120)
    if r.returncode != 0:
        raise RuntimeError((r.stderr or b"").decode("utf-8", "replace")[:400]
                           or "decode failed")
    with open(out.name, "rb") as f:
        pcm = f.read()
    os.unlink(out.name)
    if not pcm:
        raise RuntimeError("decoded to nothing - was that actually audio?")
    return pcm


def play(pcm):
    """Queue raw PCM for the stream. Returns how long it will take, in seconds."""
    _pending.put(pcm)
    return len(pcm) / (RATE * CH * WIDTH)


def is_playing():
    return _playing.is_set()


def _feed(appsrc):
    """Push one millisecond at a time, forever.

    Silence when there is nothing to say. The timing comes from the PTP clock
    and `sync=true` on the sink; this thread only has to keep the queue fed,
    and `block=true` on the appsrc is what stops it running ahead.
    """
    while True:
        try:
            pcm = _pending.get_nowait()
        except queue.Empty:
            pcm = None

        if pcm is None:
            _playing.clear()
            _push(appsrc, SILENCE)
            continue

        _playing.set()
        for i in range(0, len(pcm), FRAME_BYTES):
            chunk = pcm[i:i + FRAME_BYTES]
            if len(chunk) < FRAME_BYTES:
                chunk = chunk + b"\x00" * (FRAME_BYTES - len(chunk))
            _push(appsrc, chunk)
        _playing.clear()


_pts = 0


def _push(appsrc, data):
    """One millisecond, stamped explicitly.

    Not `do-timestamp`, which stamps on arrival and so inherits every hiccup
    of this thread - and not a free-running loop either, which would push as
    fast as Python can and leave the timestamps galloping ahead of real time.
    The cadence is exact by construction here, and `block=true` on the appsrc
    paces the thread against the sink rather than the other way round.

    AES67 receivers are unforgiving about this. A stream whose timestamps
    drift is one the NAX will accept, play, and slowly turn into clicking.
    """
    global _pts
    buf = Gst.Buffer.new_wrapped(data)
    buf.pts = _pts
    buf.duration = Gst.MSECOND
    _pts += Gst.MSECOND
    if appsrc.emit("push-buffer", buf) != Gst.FlowReturn.OK:
        # A pipeline that has stopped accepting audio is a NAX about to drop
        # the session. Loud, because silence here is indistinguishable from
        # working.
        print("[sender] appsrc refused a buffer", flush=True)


def build():
    src = local_ip(MCAST)
    iface = resolve_iface(src)
    print(f"[sender] src={src} iface={iface or '(auto)'} mcast={MCAST}:{PORT} "
          f"pt={PT} session={SESSION!r}", flush=True)

    # PTP, in userspace, never disciplining the host clock. The NAX is its own
    # grandmaster on domain 0; a client that tries to take over would drag the
    # five DM-NVX with it, so `sap.py` also pins priority in the SDP.
    print("[sender] PTP supported:", GstNet.ptp_is_supported(), flush=True)
    # An empty list means every interface, which is GStreamer's own default and
    # the only sane answer if the interface could not be identified at all.
    GstNet.ptp_init(GstNet.PTP_CLOCK_ID_NONE, [iface] if iface else [])
    clock = GstNet.PtpClock.new("ptp0", 0)
    print("[sender] waiting for PTP sync (up to 45s)…", flush=True)
    print("[sender] PTP synced:", clock.wait_for_sync(45 * Gst.SECOND), flush=True)

    sap.start(src_ip=src, mcast=MCAST, port=PORT, session_name=SESSION,
              iface_ip=src, log=lambda m: print(m, flush=True))

    # `block=true` with a small `max-bytes` is what paces the feeder: it
    # blocks in push-buffer once about 30 ms is queued, so the thread runs at
    # multicast-iface / bind-address: without them the multicast egress follows
    # whatever the host's default route happens to be, which on a box that also
    # runs VPN and bridge add-ons is not guaranteed to stay the house LAN. The
    # stream has exactly one correct interface; say so when it is known.
    # the speed the sink drains rather than the speed Python can loop.
    desc = (f"appsrc name=feed is-live=true format=time do-timestamp=false "
            f"block=true max-bytes={FRAME_BYTES * 30} "
            f"caps=audio/x-raw,format=S24BE,rate={RATE},channels={CH},layout=interleaved "
            f"! audioconvert ! audioresample "
            f"! rtpL24pay pt={PT} min-ptime=1000000 max-ptime=1000000 mtu=1452 "
            f"! udpsink host={MCAST} port={PORT} "
            + (f"multicast-iface={iface} " if iface else "")
            + f"bind-address={src} ttl-mc=16 sync=true async=false")
    print("[sender] pipeline:", desc, flush=True)
    pipe = Gst.parse_launch(desc)
    pipe.use_clock(clock)
    appsrc = pipe.get_by_name("feed")
    pipe.set_state(Gst.State.PLAYING)
    threading.Thread(target=_feed, args=(appsrc,), daemon=True).start()
    print("[sender] PLAYING (silence)", flush=True)
    return pipe


def run_forever(pipe):
    loop = GLib.MainLoop()
    bus = pipe.get_bus()
    bus.add_signal_watch()

    def on_msg(_b, m):
        if m.type == Gst.MessageType.ERROR:
            print("[sender] ERROR:", m.parse_error(), flush=True)
            loop.quit()
        elif m.type == Gst.MessageType.EOS:
            print("[sender] EOS", flush=True)
            loop.quit()

    bus.connect("message", on_msg)
    loop.run()
