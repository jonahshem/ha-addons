"""Hearing the house page itself.

A Crestron Home page goes to a *room group*. Every panel carries the groups it
belongs to in its Rava page-group list (`RG_WHOLE_HOUSE_52994,RG_1ST_FLOOR_53102,
CRESTRON`) and one SIP multicast address for paging (`227.1.1.1:1234`, from
`SIPINFO`). Rava paging is multicast: the pager announces the page to that
address and the audio goes to a multicast group; whoever belongs, listens.
Nothing asks who else is listening - so this joins the group and listens, first
only to learn exactly what a page looks like on the wire, then to hand the audio
to the NAX speakers.

This first version observes and reports. It answers nothing and sends nothing.
"""
import socket
import struct
import threading
import time
from collections import deque

import rtp
import sip


class PageListener:
    def __init__(self, bridge, group="227.1.1.1", port=1234, log=None, relay=None):
        self.b = bridge
        self.group = group
        self.port = int(port)
        self.log = log or print
        # Where a live page is sent on to. Off unless configured.
        self.relay_cfg = relay or {}
        self.relayed = {"pages": 0, "bytes": 0, "last": None}
        self.pages = deque(maxlen=40)         # what was seen, newest last
        self.raw = {"sip": 0, "rtp": 0, "other": 0}
        self.media = {}                       # (group, port) -> {"sock", "packets", "pt", "first", "last", "page"}
        self.lock = threading.Lock()
        self.sock = None

    def _join(self, group, port):
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        s.bind(("", port))
        mreq = socket.inet_aton(group) + socket.inet_aton(self.b.address)
        s.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP, mreq)
        s.settimeout(0.5)
        return s

    def start(self):
        try:
            self.sock = self._join(self.group, self.port)
        except OSError as e:
            self.log(f"pages: cannot join {self.group}:{self.port} on {self.b.address}: {e}")
            return
        threading.Thread(target=self._loop, daemon=True, name="pages").start()
        self.b.note(f"listening for pages on {self.group}:{self.port}")

    def _loop(self):
        while True:
            try:
                data, addr = self.sock.recvfrom(65535)
            except socket.timeout:
                self._reap()
                continue
            except OSError:
                time.sleep(0.1)
                continue
            self._packet(data, addr)

    def _packet(self, data, addr):
        head = data[:12]
        if head[:1].isalpha() and (b"SIP/2.0" in data[:200]):
            self.raw["sip"] += 1
            msg = sip.parse(data)
            if msg is None:
                return
            self._sip(msg, data, addr)
            return
        pkt = rtp.unpack(data)
        if pkt:
            self.raw["rtp"] += 1
            self._rtp((self.group, self.port), pkt, addr)
            return
        self.raw["other"] += 1

    def _sip(self, msg, raw, addr):
        what = msg.method or f"{msg.status} {msg.reason}"
        offer = sip.sdp_parse(msg.body) if msg.body else {}
        audio = (offer or {}).get("audio")
        entry = {
            "t": time.time(), "from_host": f"{addr[0]}:{addr[1]}", "what": what, "uri": msg.uri,
            "from": msg.get("from"), "to": msg.get("to"), "call_id": msg.call_id,
            "user_agent": msg.get("user-agent"), "subject": msg.get("subject"),
            "alert_info": msg.get("alert-info"), "content_type": msg.get("content-type"),
            "audio": ({"address": audio.get("address"), "port": audio.get("port"),
                       "payloads": audio.get("payloads"), "rtpmaps": audio.get("rtpmaps"),
                       "direction": audio.get("direction")} if audio else None),
            "headers": {k: v for k, v in msg.headers.items()
                        if k not in ("via", "from", "to", "call-id", "cseq", "content-length", "max-forwards")},
            "body": msg.body.decode("utf-8", "replace")[:1500],
        }
        with self.lock:
            self.pages.append(entry)
        self.b.note(f"page seen: {what} {msg.uri or ''} from {addr[0]} to {msg.get('to')}"
                    + (f" - audio {audio.get('address')}:{audio.get('port')} pt {audio.get('payloads')}" if audio else ""))
        if self.b.log_sip:
            self.log(f"<<< page {addr[0]}:{addr[1]}\n{raw.decode('utf-8', 'replace').rstrip()}")
        # Follow the audio: if the page's SDP names a multicast group, join it and count.
        if audio and audio.get("address") and audio.get("port"):
            a = audio["address"]
            try:
                first = int(a.split(".")[0])
            except ValueError:
                first = 0
            if 224 <= first <= 239 and (a, audio["port"]) not in self.media and (a, audio["port"]) != (self.group, self.port):
                try:
                    s = self._join(a, audio["port"])
                except OSError as e:
                    self.log(f"pages: cannot join audio group {a}:{audio['port']}: {e}")
                    return
                rec = {"sock": s, "packets": 0, "pt": None, "first": None, "last": None,
                       "page": msg.call_id, "group": a, "port": audio["port"], "bytes": 0, "codec": audio.get("rtpmaps")}
                with self.lock:
                    self.media[(a, audio["port"])] = rec
                threading.Thread(target=self._media_loop, args=(rec,), daemon=True).start()

    def _media_loop(self, rec):
        s = rec["sock"]
        while True:
            try:
                data, addr = s.recvfrom(65535)
            except socket.timeout:
                if rec["last"] and time.time() - rec["last"] > 3:
                    break
                if not rec["first"] and time.time() - rec.get("opened", time.time()) > 120:
                    break
                continue
            except OSError:
                break
            pkt = rtp.unpack(data)
            if not pkt:
                continue
            self._rtp((rec["group"], rec["port"]), pkt, addr, rec)
        self._relay_stop(rec)
        try:
            s.close()
        except OSError:
            pass
        with self.lock:
            self.media.pop((rec["group"], rec["port"]), None)
        if rec["packets"]:
            dur = (rec["last"] or 0) - (rec["first"] or 0)
            self.b.note(f"page audio on {rec['group']}:{rec['port']}: {rec['packets']} packets, pt {rec['pt']}, "
                        f"{rec['bytes']} bytes, {dur:.1f}s, from {rec.get('src')}")
            with self.lock:
                self.pages.append({"t": time.time(), "what": "AUDIO", "group": rec["group"], "port": rec["port"],
                                   "packets": rec["packets"], "pt": rec["pt"], "bytes": rec["bytes"],
                                   "seconds": round(dur, 1), "src": rec.get("src"), "page": rec["page"]})

    def _rtp(self, where, pkt, addr, rec=None):
        now = time.time()
        if rec is None:
            # RTP straight on the paging address itself: count it, note the source.
            rec = self.media.get(where)
            if rec is None:
                rec = {"sock": None, "packets": 0, "pt": None, "first": None, "last": None, "page": None,
                       "group": where[0], "port": where[1], "bytes": 0}
                with self.lock:
                    self.media[where] = rec
        first_packet = rec["packets"] == 0
        rec["packets"] += 1
        rec["bytes"] += len(pkt["payload"])
        rec["pt"] = pkt["pt"]
        rec["src"] = f"{addr[0]}:{addr[1]}"
        rec["first"] = rec["first"] or now
        rec["last"] = now
        if first_packet:
            self._relay_start(rec)
        self._relay_push(rec, pkt)

    # -- sending a live page on to the NAX speakers -------------------------
    def _relay_start(self, rec):
        """A page has started. Open the stream to the AES67 sender.

        Only for G.711 u-law (payload type 0), which is what a Crestron page
        is; anything else is left alone rather than mistranslated.
        """
        cfg = self.relay_cfg
        if not cfg.get("enabled") or rec.get("relay") is not None:
            return
        import pageaudio
        rec["conv"] = pageaudio.Ulaw48k()
        rec["relay"] = pageaudio.PageRelay(cfg.get("url") or "", cfg.get("token") or "",
                                           cfg.get("zones") or [], log=self.log)
        # Started lazily on the first u-law packet, so a non-audio blip on the
        # group does not route the house's zones for nothing.
        rec["relay_armed"] = True

    def _relay_push(self, rec, pkt):
        relay = rec.get("relay")
        if relay is None or pkt["pt"] != 0 or not pkt["payload"]:
            return
        if rec.get("relay_armed"):
            rec["relay_armed"] = False
            if not relay.start():
                rec["relay"] = None
                return
            self.b.note(f"page: relaying to the NAX zones {relay.zones}")
        relay.push(rec["conv"].convert(pkt["payload"]))

    def _relay_stop(self, rec):
        relay = rec.get("relay")
        if relay is None:
            return
        rec["relay"] = None
        if not relay.open:
            return
        out = relay.stop()
        self.relayed["pages"] += 1
        self.relayed["bytes"] += (out or {}).get("bytes", 0)
        self.relayed["last"] = {"t": time.time(), **(out or {})}
        self.b.note(f"page: relay finished, {(out or {}).get('bytes', 0)} bytes to the NAX zones")

    def _reap(self):
        # RTP seen directly on the paging address, gone quiet: report it.
        now = time.time()
        for key, rec in list(self.media.items()):
            if rec["sock"] is None and rec["last"] and now - rec["last"] > 3:
                self._relay_stop(rec)
                dur = rec["last"] - rec["first"]
                self.b.note(f"page audio on {key[0]}:{key[1]}: {rec['packets']} packets, pt {rec['pt']}, "
                            f"{rec['bytes']} bytes, {dur:.1f}s, from {rec.get('src')}")
                with self.lock:
                    self.pages.append({"t": now, "what": "AUDIO", "group": key[0], "port": key[1],
                                       "packets": rec["packets"], "pt": rec["pt"], "bytes": rec["bytes"],
                                       "seconds": round(dur, 1), "src": rec.get("src")})
                    self.media.pop(key, None)

    def public(self):
        with self.lock:
            return {"group": f"{self.group}:{self.port}", "counters": dict(self.raw),
                    "listening_media": [f"{k[0]}:{k[1]}" for k in self.media],
                    "relay": {"enabled": bool(self.relay_cfg.get("enabled")),
                              "zones": self.relay_cfg.get("zones") or [],
                              **self.relayed},
                    "seen": list(self.pages)}
