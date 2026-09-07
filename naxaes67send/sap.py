"""SAP/SDP announcer (RFC 2974) so the DM NAX autodiscovers our AES67 stream.

Unchanged in substance from the version proven at 14 Malke on 2026-09-03. The
NAX only listens for SAP; nothing on that network was announcing, which is why
the stream was invisible until this existed.
"""
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


def build_sdp(src_ip, mcast, port, session_name, ch=2, rate=48000, gm=GM):
    sid = int(time.time())
    return "\r\n".join([
        "v=0",
        f"o=- {sid} {sid} IN IP4 {src_ip}",
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


def build_sap(src_ip, sdp, msg_hash):
    # V=1, A=0 (IPv4), R=0, T=0 (announce), E=0, C=0 -> 0x20; auth_len = 0
    hdr = struct.pack("!BBH", 0x20, 0x00, msg_hash) + socket.inet_aton(src_ip)
    return hdr + b"application/sdp\x00" + sdp.encode("utf-8")


def announce_forever(src_ip, mcast, port, session_name, iface_ip=None, gm=GM,
                     targets=(("239.255.255.255", 9875), ("224.2.127.254", 9875)),
                     interval=10.0, log=print):
    sdp = build_sdp(src_ip, mcast, port, session_name, gm=gm)
    pkt = build_sap(src_ip, sdp, random.randint(1, 0xFFFF))
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 16)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
    if iface_ip:
        s.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_IF, socket.inet_aton(iface_ip))
    log(f"[sap] announcing {session_name!r} {mcast}:{port} -> {targets} every {interval}s")
    log("[sap] SDP:\n" + sdp)
    n = 0
    while True:
        for dst in targets:
            try:
                s.sendto(pkt, dst)
            except Exception as e:
                log(f"[sap] send {dst} failed: {e}")
        n += 1
        if n <= 3 or n % 30 == 0:
            log(f"[sap] announcement #{n} sent")
        time.sleep(interval)


def start(src_ip, mcast, port, session_name, iface_ip=None, gm=GM, log=print):
    t = threading.Thread(target=announce_forever, daemon=True,
                         kwargs=dict(src_ip=src_ip, mcast=mcast, port=port,
                                     session_name=session_name,
                                     iface_ip=iface_ip, gm=gm, log=log))
    t.start()
    return t
