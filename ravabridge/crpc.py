"""Staying Online: registering with the Crestron Home processor as an intercom device.

A Crestron Home touch panel is "online" (and therefore a valid page target) only while it
holds a session on the processor's secure RPC port. This registers the bridge as one or more
virtual touch panels - the fake devices added to the processor's config by
`Tools/mc4r_fake_panel.py` - so the rooms they live in become pageable and their audio is
addressed to the bridge's own SIP URI.

Decoded from a live TSW-770R (see `Tools/crpc_samples/REGISTRATION.md`):

  TLS -> [0x26 connect frame: 212-byte fixed header + length-prefixed "clientdevice:<PIN>"]
         [0x14 frame: Crpc.Register JSON, uuid = the device's RpcClientGuid]
      <- 0x27 ack, 0x14 {result:{connectionStatus:New|Resumed}}
  then a 3 s heartbeat, IRpcHouse.ReportClientAssignedRoom, IRpcIntercomDevice.SetSipUri.

The processor maps the register `uuid` to a device via its DeviceManifest, so each virtual
device registers with the RpcClientGuid stored for it there. Standard library only.
"""
import json
import socket
import ssl
import struct
import threading
import time

PORT = 50001
# The 0x26 connect frame's fixed header, verbatim from the captured panel session:
# 00 03 40 F9 01 then zero padding to 212 bytes. Only the auth string that follows changes.
CONNECT_HEADER = bytes([0x00, 0x03, 0x40, 0xF9, 0x01]) + b"\x00" * 207
HEARTBEAT = b"\x0d\x00\x02\x00\x00"
HEARTBEAT_SECS = 3


def _be2(n):
    return struct.pack(">H", n)


def _data_frame(obj):
    body = b"\x01" + json.dumps(obj, separators=(",", ":")).encode()
    return b"\x14" + _be2(len(body)) + body


def _frames(buf):
    out = []
    while len(buf) >= 3:
        n = struct.unpack(">H", buf[1:3])[0]
        if len(buf) < 3 + n:
            break
        out.append((buf[0], buf[3:3 + n]))
        buf = buf[3 + n:]
    return out, buf


class VirtualDevice:
    """One registered intercom endpoint: a fake panel the processor believes is online."""

    def __init__(self, mgr, cfg):
        self.mgr = mgr
        self.name = str(cfg.get("name") or "RavaBridge")
        self.uuid = str(cfg.get("uuid") or "").strip()
        self.room_id = int(cfg.get("room_id") or 0)
        self.sip_uri = str(cfg.get("sip_uri") or "").strip()
        self.model = str(cfg.get("model") or "TSW-770R")
        self.online = False
        self.status = "new"
        self.last_error = None
        self.registered_at = None
        self._id = 100
        self._sock = None
        self._stop = threading.Event()

    @property
    def ok(self):
        return bool(self.uuid and self.room_id and self.sip_uri)

    def public(self):
        return {"name": self.name, "uuid": self.uuid, "room": self.room_id, "sip": self.sip_uri,
                "online": self.online, "status": self.status, "error": self.last_error}

    def _next_id(self):
        self._id += 1
        return self._id

    def _register_frame(self):
        auth = f"clientdevice:{self.mgr.pin}".encode()
        connect = b"\x26" + _be2(len(CONNECT_HEADER) + 1 + len(auth)) + CONNECT_HEADER + bytes([len(auth)]) + auth
        register = _data_frame({
            "method": "Crpc.Register", "id": self._next_id(), "jsonrpc": "2.0",
            "params": {
                "ver": "1.0", "appVersion": "4.12.7", "formFactor": "touchscreen", "format": "JSON",
                "deviceMake": "Crestron", "type": "cip-direct/json-rpc", "maxPacketSize": 65535,
                "encoding": "UTF-8", "uuid": self.uuid, "osVersion": "3.003.0015", "appType": "phoenix",
                "lastUpdateStatus": "Unavailable", "name": self.name, "deviceModel": self.model,
                "clientData": {"extraFeatures": ["Intercom"]},
            },
        })
        return connect + register

    def start(self):
        threading.Thread(target=self._run, daemon=True, name=f"crpc:{self.name}").start()

    def stop(self):
        self._stop.set()
        if self._sock:
            try:
                self._sock.close()
            except OSError:
                pass

    def _run(self):
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        while not self._stop.is_set():
            try:
                self._session(ctx)
            except Exception as e:
                self.last_error = repr(e)
                self.status = "error"
            self.online = False
            if not self._stop.is_set():
                self._stop.wait(5)              # reconnect after a short backoff

    def _session(self, ctx):
        self.status = "connecting"
        ss = ctx.wrap_socket(socket.create_connection((self.mgr.host, PORT), timeout=8),
                             server_hostname=self.mgr.host)
        self._sock = ss
        ss.sendall(self._register_frame())
        self.status = "registering"
        ss.settimeout(1.0)
        buf = b""
        last_hb = 0
        did_setup = False
        while not self._stop.is_set():
            now = time.time()
            if self.online and now - last_hb >= HEARTBEAT_SECS:
                try:
                    ss.sendall(HEARTBEAT)
                except OSError:
                    break
                last_hb = now
            try:
                chunk = ss.recv(65535)
                if not chunk:
                    break
                buf += chunk
            except socket.timeout:
                chunk = None
            except OSError:
                break
            if chunk:
                got, buf = _frames(buf)
                for t, body in got:
                    if t == 0x14 and b'"connectionStatus"' in body:
                        try:
                            st = json.loads(body[1:].decode("utf-8", "replace"))
                            self.status = st.get("result", {}).get("connectionStatus", "registered")
                        except ValueError:
                            self.status = "registered"
                        self.online = True
                        self.registered_at = now
                        last_hb = now
                        self.mgr.log(f"crpc: {self.name} registered ({self.status})")
            if self.online and not did_setup:
                ss.sendall(_data_frame({"method": "IRpcHouse.ReportClientAssignedRoom", "id": self._next_id(),
                                        "jsonrpc": "2.0", "params": {"roomId": self.room_id}}))
                ss.sendall(_data_frame({"method": "IRpcIntercomDevice.SetSipUri", "id": self._next_id(),
                                        "jsonrpc": "2.0", "params": {"sipUri": self.sip_uri}}))
                ss.sendall(_data_frame({"method": "IRpcIntercom.RequestPageableRoomGroupsChangedEvents",
                                        "id": self._next_id(), "jsonrpc": "2.0", "params": {"minimumTime": 500}}))
                did_setup = True
                self.mgr.log(f"crpc: {self.name} online, room {self.room_id}, sip {self.sip_uri}")
        try:
            ss.close()
        except OSError:
            pass
        self._sock = None
        self.online = False


class CrpcManager:
    def __init__(self, cfg, log=print):
        self.log = log
        c = cfg.get("crpc") or {}
        self.host = str(c.get("host") or "").strip()
        self.pin = str(c.get("pin") or "2129918115")
        self.devices = [VirtualDevice(self, d) for d in (cfg.get("intercom_devices") or [])]
        self.devices = [d for d in self.devices if d.ok]

    @property
    def enabled(self):
        return bool(self.host and self.devices)

    def start(self):
        if not self.enabled:
            return
        self.log(f"crpc: registering {len(self.devices)} device(s) with the processor at {self.host}")
        for d in self.devices:
            d.start()

    def stop(self):
        for d in self.devices:
            d.stop()

    def public(self):
        return {"host": self.host, "devices": [d.public() for d in self.devices]}
