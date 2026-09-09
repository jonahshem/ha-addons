"""Ask the Crestron Home processor to resume a room's music - the way its own app does.

The amplifier is not what pauses the music. Crestron Home is: it watches the
zone's route, sees the zone leave its player during an announcement, and pauses
that player (processor log: "Externally cleared route ... Media Player state
changed to Paused"). Putting the route back is not enough - the app resumes by
calling IRpcMediaSources.SendCommand(sourceId, "Play") over CRPC, and so do we.

CRPC framing and registration are the ones RavaBridge decoded from a real
TSW-770R (RavaBridge/crpc.py, Tools/crpc_samples/REGISTRATION.md): TLS to port
50001, a 0x26 connect frame carrying "clientdevice:<PIN>", then JSON-RPC in 0x14
frames with a 3 s heartbeat. A brand-new client uuid registers fine, so this
add-on holds its own session rather than sharing RavaBridge's. Standard library.
"""
import json
import os
import socket
import ssl
import struct
import threading
import time
import uuid as uuidlib

PORT = 50001
CONNECT_HEADER = bytes([0x00, 0x03, 0x40, 0xF9, 0x01]) + b"\x00" * 207
HEARTBEAT = b"\x0d\x00\x02\x00\x00"
HEARTBEAT_SECS = 3


def _frame(obj):
    body = b"\x01" + json.dumps(obj, separators=(",", ":")).encode()
    return b"\x14" + struct.pack(">H", len(body)) + body


def _frames(buf):
    out = []
    while len(buf) >= 3:
        n = struct.unpack(">H", buf[1:3])[0]
        if len(buf) < 3 + n:
            break
        out.append((buf[0], buf[3:3 + n]))
        buf = buf[3 + n:]
    return out, buf


def _objects(text):
    """Complete top-level JSON objects at the front of `text`; replies span frames."""
    out = []
    while True:
        start = text.find("{")
        if start < 0:
            return out, ""
        depth, in_str, esc, end = 0, False, False, -1
        for i in range(start, len(text)):
            c = text[i]
            if in_str:
                if esc:
                    esc = False
                elif c == "\\":
                    esc = True
                elif c == '"':
                    in_str = False
            elif c == '"':
                in_str = True
            elif c == "{":
                depth += 1
            elif c == "}":
                depth -= 1
                if depth == 0:
                    end = i
                    break
        if end < 0:
            return out, text[start:]
        try:
            out.append(json.loads(text[start:end + 1]))
        except ValueError:
            pass
        text = text[end + 1:]


class CrestronHome:
    """One registered session on the processor, kept open between pages."""

    def __init__(self, host, pin, uuid_file="/data/crpc_uuid", name="NAX Announce", log=print):
        self.host, self.pin, self.name, self.log = host, str(pin), name, log
        self.uuid = self._identity(uuid_file)
        self._ss = None
        self._buf, self._text = b"", ""
        self._replies = {}
        self._id = 1000
        self._last_hb = 0.0
        self._lock = threading.Lock()
        # The live view of what is playing, kept by the warm thread. A page
        # reads this instantly; asking the processor at page time put its two
        # replies (200 KB together, about a second warm) in front of the audio.
        self._sub, self._sub_rev, self._sub_at = None, 0, 0.0      # rooms + names
        self._state, self._state_rev, self._state_at = None, 0, 0.0  # room/source states
        # Registering costs about six seconds (the handshake, then two large
        # replies on a cold connection). Paid at startup and kept warm with the
        # processor's own heartbeat, so a page never pays it: the first page
        # after a restart started its audio 9.5 s in instead of 2.5 s.
        threading.Thread(target=self._keep_warm, daemon=True, name="crpc-warm").start()

    def _keep_warm(self):
        """Hold the session, and keep the media snapshot current.

        Both reads take a revstamp and answer null when nothing has changed
        since it - measured at 0.5 s for the null, against 0.6 s for a full
        121 KB state - so polling the state every two seconds costs the
        processor almost nothing, and the snapshot is never more than a couple
        of seconds behind the house. The room list changes when an installer
        does something, so it is refreshed every minute.
        """
        while True:
            try:
                with self._lock:
                    if self._ss is None:
                        self._connect()
                    now = time.time()
                    if now - self._sub_at > 60:
                        sub = self._call("IRpcMedia.GetSubsystem", {"systemRevstamp": self._sub_rev})
                        if sub:
                            self._sub, self._sub_rev = sub, sub.get("SystemRevstamp") or 0
                        self._sub_at = now
                    if now - self._state_at > 2:
                        st = self._call("IRpcMedia.GetSubsystemState", {"stateRevstamp": self._state_rev})
                        if st:
                            self._state, self._state_rev = st, st.get("StateRevstamp") or 0
                        self._state_at = now
                    self._pump(0.2)          # sends the heartbeat when it is due
            except Exception as e:
                self.close()
                self.log(f"[crpc] session to {self.host} dropped ({type(e).__name__}); retrying in 10s")
                time.sleep(10)
                continue
            time.sleep(0.5)

    @staticmethod
    def _identity(path):
        """A stable client uuid, so the processor sees one device across restarts."""
        try:
            with open(path) as f:
                v = f.read().strip()
            if v:
                return v
        except OSError:
            pass
        v = str(uuidlib.uuid4())
        try:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "w") as f:
                f.write(v)
        except OSError:
            pass
        return v

    # -- session -----------------------------------------------------------
    def _connect(self):
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        ss = ctx.wrap_socket(socket.create_connection((self.host, PORT), timeout=8),
                             server_hostname=self.host)
        auth = f"clientdevice:{self.pin}".encode()
        ss.sendall(b"\x26" + struct.pack(">H", len(CONNECT_HEADER) + 1 + len(auth)) + CONNECT_HEADER
                   + bytes([len(auth)]) + auth)
        ss.sendall(_frame({"method": "Crpc.Register", "id": self._next(), "jsonrpc": "2.0", "params": {
            "ver": "1.0", "appVersion": "4.12.7", "formFactor": "touchscreen", "format": "JSON",
            "deviceMake": "Crestron", "type": "cip-direct/json-rpc", "maxPacketSize": 65535,
            "encoding": "UTF-8", "uuid": self.uuid, "osVersion": "3.003.0015", "appType": "phoenix",
            "lastUpdateStatus": "Unavailable", "name": self.name, "deviceModel": "TSW-770",
            "clientData": {"extraFeatures": []}}}))
        ss.settimeout(0.5)
        self._ss, self._buf, self._text, self._replies = ss, b"", "", {}
        self._pump(2.5)
        if not any("connectionStatus" in json.dumps(r) for r in self._replies.values()):
            raise ConnectionError("processor did not acknowledge the registration")
        self.log(f"[crpc] registered with {self.host} as {self.name!r}")

    def _next(self):
        self._id += 1
        return self._id

    def _pump(self, seconds):
        end = time.time() + seconds
        while time.time() < end:
            try:
                data = self._ss.recv(65535)
                if not data:
                    raise ConnectionError("processor closed the session")
                self._buf += data
            except (socket.timeout, ssl.SSLWantReadError):
                pass
            got, self._buf = _frames(self._buf)
            for t, body in got:
                if t == 0x14 and len(body) > 1:
                    self._text += body[1:].decode("utf-8", "replace")
            objs, self._text = _objects(self._text)
            for d in objs:
                if "id" in d:
                    self._replies[d["id"]] = d
            if time.time() - self._last_hb > HEARTBEAT_SECS:
                self._ss.sendall(HEARTBEAT)
                self._last_hb = time.time()

    def _call(self, method, params, wait=12.0):
        i = self._next()
        self._ss.sendall(_frame({"method": method, "id": i, "jsonrpc": "2.0", "params": params}))
        end = time.time() + wait
        while time.time() < end:
            self._pump(0.5)
            if i in self._replies:
                r = self._replies.pop(i)
                if r.get("error"):
                    raise RuntimeError(f"{method}: {r['error'].get('message')}")
                return r.get("result")
        raise TimeoutError(f"{method}: no reply in {wait:.0f}s")

    def call(self, method, params, wait=12.0):
        """One call, on a session that is rebuilt once if it has gone stale."""
        with self._lock:
            for attempt in (1, 2):
                try:
                    if self._ss is None:
                        self._connect()
                    return self._call(method, params, wait)
                except (OSError, ConnectionError, TimeoutError) as e:
                    self.close()
                    if attempt == 2:
                        raise
                    self.log(f"[crpc] session to {self.host} failed ({type(e).__name__}), reconnecting")

    def close(self):
        try:
            if self._ss:
                self._ss.close()
        except OSError:
            pass
        self._ss = None

    # -- media ---------------------------------------------------------------
    def rooms_playing(self):
        """{room name (lower): source id} for every media room whose source is playing.

        Names are the join between the amplifier and the processor: a zone the
        installer called "Deck" on the amplifier is the media room called
        "Deck" in Crestron Home, because both came off the same room list.
        """
        # The snapshot, if the warm thread has one that is recent; otherwise a
        # live read - the first page after a restart, or a processor that has
        # been unreachable.
        sub, st = self._sub, self._state
        if not sub or not st or time.time() - self._state_at > 15:
            sub = self.call("IRpcMedia.GetSubsystem", {"systemRevstamp": 0})
            st = self.call("IRpcMedia.GetSubsystemState", {"stateRevstamp": 0})
        names = {r["Id"]: (r.get("Name") or "").strip() for r in sub.get("Rooms", [])}
        playing = {s["Id"] for s in st.get("SourceStates", [])
                   if "Playing" in (s.get("PlayerState") or [])}
        out = {}
        for rs in st.get("RoomStates", []):
            src = rs.get("CurrentAudioSourceId") or rs.get("CurrentSourceId")
            if src in playing and names.get(rs["Id"]):
                out[names[rs["Id"]].lower()] = src
        return out

    def play(self, source_id):
        """What the app sends when the user presses Play."""
        self.call("IRpcMediaSources.SendCommand", {
            "sourceId": int(source_id), "command": "Play", "triggerType": "Pulse",
            "targetId": 0, "useAliasing": False}, wait=8.0)
