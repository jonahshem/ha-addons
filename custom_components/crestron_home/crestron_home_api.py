"""One client for the Crestron Home CWS REST API — the `/cws/api` "Pyng" service.

Canonical source in the repo; **vendored** into the projects that use it (HomeUI on CT 119,
the CH2HA Home Assistant integration). Keep this file the single place the endpoint paths,
the scaling, the auth dance and the banner/​error handling live, so a firmware quirk is fixed
once. Full protocol reference: `Docs/CrestronHomeRestApi.md`.

Two things about this service that every caller gets wrong once:

* **Two secrets.** Login sends the installer's token in `Crestron-RestAPI-AuthToken` and gets
  back an **`authkey`** (lowercase — the doc sample says `AuthKey`, the wire says `authkey`).
  Every other call sends `Crestron-RestAPI-AuthKey`. The key idles out after 10 minutes; a
  401/511 means "log in again", not "wrong token".
* **An unknown path answers HTTP 200 with the Pyng banner, not 404.** So a typo, or an
  endpoint this firmware does not have, looks like success. `_call` treats a banner as
  "not available" rather than data.

Stdlib only (HomeUI ships no third-party deps). Synchronous: an async caller (CH2HA) either
runs it in an executor or reuses the pure helpers (`pct_to_raw`, `raw_to_pct`, the `paths`
builders) inside its own aiohttp request. Transport is injectable — the default talks HTTPS
straight to a processor; HomeUI passes a transport that tunnels through the fleet Hub relay.
"""
import json
import ssl
import urllib.error
import urllib.request

RAW_MAX = 65535               # lights level / shades position full scale
DEFAULT_TIMEOUT = 15


# ---- scaling: the processor's units vs the units people think in ---------------------

def pct_to_raw(pct):
    """0–100 (%) -> 0–65535. Clamped. None -> None."""
    if pct is None:
        return None
    return max(0, min(RAW_MAX, int(round(float(pct) / 100.0 * RAW_MAX))))


def raw_to_pct(raw):
    """0–65535 -> 0–100 (%), rounded. None -> None."""
    if raw is None:
        return None
    return max(0, min(100, int(round(float(raw) / RAW_MAX * 100.0))))


def temp_from_deci(value):
    """Crestron reports temperatures as integer tenths: 770 -> 77.0. None-safe."""
    if value is None:
        return None
    try:
        return int(value) / 10.0
    except (TypeError, ValueError):
        return None


def temp_to_deci(value):
    """77 or 77.0 -> 770 for a setpoint write."""
    if value is None:
        return None
    return int(round(float(value) * 10))


class CrestronError(Exception):
    """A REST-layer failure. `code` is the Crestron `errorSource` when there is one,
    or a short symbol ('BANNER', 'HTTP', 'TRANSPORT', 'NO_KEY'); `status` the HTTP code."""
    def __init__(self, message, code="ERROR", status=0):
        super().__init__(message)
        self.message = message
        self.code = code
        self.status = status


# ---- endpoint paths, as pure builders (method, path, body) ---------------------------
#
# Kept separate from the client so an async caller can reuse them without this file's I/O.

class paths:
    login = ("GET", "/login", None)
    logout = ("GET", "/logout", None)
    rooms = ("GET", "/rooms", None)
    devices = ("GET", "/devices", None)
    scenes = ("GET", "/scenes", None)
    lights = ("GET", "/lights", None)
    shades = ("GET", "/shades", None)
    thermostats = ("GET", "/thermostats", None)
    doorlocks = ("GET", "/doorlocks", None)
    sensors = ("GET", "/sensors", None)
    securitydevices = ("GET", "/securitydevices", None)
    quickactions = ("GET", "/quickactions", None)
    mediarooms = ("GET", "/mediarooms", None)

    @staticmethod
    def one(kind, rid):
        return ("GET", "/%s/%s" % (kind, rid), None)

    @staticmethod
    def lights_set(items):
        # items: iterable of (id, raw_level, time_ms)
        return ("POST", "/lights/SetState",
                {"lights": [{"id": i, "level": lvl, "time": t} for (i, lvl, t) in items]})

    @staticmethod
    def shades_set(items):
        # items: iterable of (id, raw_position)
        return ("POST", "/shades/SetState",
                {"shades": [{"id": i, "position": p} for (i, p) in items]})

    @staticmethod
    def scene_recall(sid):
        return ("POST", "/scenes/recall/%s" % sid, None)

    @staticmethod
    def thermostat_setpoint(tid, setpoints):
        # setpoints: iterable of (type, temp_deci)
        return ("POST", "/thermostats/SetPoint",
                {"id": tid, "setpoints": [{"type": t, "temperature": temp} for (t, temp) in setpoints]})

    @staticmethod
    def thermostat_mode(items):
        return ("POST", "/thermostats/mode",
                {"thermostats": [{"id": i, "mode": m} for (i, m) in items]})

    @staticmethod
    def thermostat_fanmode(items):
        return ("POST", "/thermostats/fanmode",
                {"thermostats": [{"id": i, "mode": m} for (i, m) in items]})

    @staticmethod
    def thermostat_schedule(items):
        return ("POST", "/thermostats/schedule",
                {"thermostats": [{"id": i, "mode": m} for (i, m) in items]})

    @staticmethod
    def doorlock(action, lid):
        return ("POST", "/doorlocks/%s/%s" % (action, lid), None)

    @staticmethod
    def mediaroom(rid, verb, arg=None):
        p = "/mediarooms/%s/%s" % (rid, verb)
        if arg is not None:
            p += "/%s" % arg
        return ("POST", p, None)


def _looks_like_banner(text):
    if not text:
        return False
    return "Pyng Rest API" in text or "Refer to the user" in text


class CrestronHome:
    """A logged-in session against one Crestron Home processor.

    Direct use: `CrestronHome(host, token)` talks HTTPS to `https://{host}/cws/api`.
    Relay use: pass `transport=fn` where `fn(method, path, body) -> (status:int, text:str)`
    routes the call however the caller reaches the processor (e.g. the fleet Hub). With a
    transport supplied, `token` is optional and login/re-login are the transport's problem.
    """

    def __init__(self, host=None, token=None, *, transport=None, timeout=DEFAULT_TIMEOUT, log=None):
        self.host = host
        self.token = token
        self.timeout = timeout
        self.log = log or (lambda *a, **k: None)
        self._authkey = None
        if transport is not None:
            self._transport = transport
            self._direct = False
        else:
            if not host:
                raise ValueError("host is required for the direct transport")
            self._transport = self._https_transport
            self._direct = True
            self._ctx = ssl.create_default_context()
            self._ctx.check_hostname = False
            self._ctx.verify_mode = ssl.CERT_NONE

    # -- transport ---------------------------------------------------------------------

    def _https_transport(self, method, path, body):
        url = "https://%s/cws/api%s" % (self.host, path)
        headers = {"User-Agent": "Mozilla/5.0 (CrestronHome)", "Accept": "application/json"}
        if path == "/login":
            headers["Crestron-RestAPI-AuthToken"] = self.token or ""
        elif self._authkey:
            headers["Crestron-RestAPI-AuthKey"] = self._authkey
        data = None
        if body is not None:
            data = json.dumps(body).encode()
            headers["Content-Type"] = "application/json"
        req = urllib.request.Request(url, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=self.timeout, context=self._ctx) as r:
                return r.status, r.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as e:
            return e.code, e.read().decode("utf-8", "replace")
        except Exception as e:
            raise CrestronError("transport to %s failed: %s" % (self.host, e), code="TRANSPORT")

    # -- auth (direct transport only) --------------------------------------------------

    def login(self):
        """Get a fresh authkey. No-op for a relay transport (it owns auth)."""
        if not self._direct:
            return True
        status, text = self._transport("GET", "/login", None)
        if status != 200:
            raise CrestronError("login HTTP %d" % status, code="HTTP", status=status)
        try:
            data = json.loads(text)
        except ValueError:
            raise CrestronError("login returned non-JSON", code="HTTP", status=status)
        self._authkey = data.get("authkey") or data.get("AuthKey")
        if not self._authkey:
            raise CrestronError("login returned no authkey", code="NO_KEY", status=status)
        return True

    # -- one call, with auth, re-login and banner/error handling -----------------------

    def _call(self, spec):
        method, path, body = spec
        if self._direct and not self._authkey and path != "/login":
            self.login()
        status, text = self._transport(method, path, body)
        # An expired key (direct transport): re-login once and retry.
        if self._direct and status in (401, 511) and path != "/login":
            self.log("[crestron] %s -> %d, re-logging in" % (path, status))
            self._authkey = None
            self.login()
            status, text = self._transport(method, path, body)
        if _looks_like_banner(text):
            raise CrestronError("%s is not available on this processor (Pyng banner)" % path,
                                code="BANNER", status=status)
        if status < 200 or status >= 300:
            raise self._error(text, status, path)
        if not text:
            return {}
        try:
            return json.loads(text)
        except ValueError:
            raise CrestronError("%s returned non-JSON" % path, code="HTTP", status=status)

    @staticmethod
    def _error(text, status, path):
        try:
            j = json.loads(text)
            return CrestronError(j.get("errorMessage") or ("HTTP %d" % status),
                                 code=j.get("errorSource", "HTTP"), status=status)
        except ValueError:
            return CrestronError("%s HTTP %d" % (path, status), code="HTTP", status=status)

    # -- reads (return the inner list where the payload wraps one) ---------------------

    def rooms(self):            return self._call(paths.rooms).get("rooms", [])
    def devices(self):          return self._call(paths.devices).get("devices", [])
    def device(self, rid):      return self._call(paths.one("devices", rid)).get("devices", [])
    def scenes(self):           return self._call(paths.scenes).get("scenes", [])
    def lights(self):           return self._call(paths.lights).get("lights", [])
    def light(self, rid):       return self._call(paths.one("lights", rid)).get("lights", [])
    def shades(self):           return self._call(paths.shades).get("shades", [])
    def thermostats(self):      return self._call(paths.thermostats).get("thermostats", [])
    def doorlocks(self):        return self._call(paths.doorlocks).get("doorLocks", [])
    def sensors(self):          return self._call(paths.sensors).get("sensors", [])
    def security_devices(self): return self._call(paths.securitydevices).get("securityDevices", [])
    def quickactions(self):     return self._call(paths.quickactions).get("quickActions", [])
    def mediarooms(self):       return self._call(paths.mediarooms).get("mediaRooms", [])
    def mediaroom(self, rid):   return self._call(paths.one("mediarooms", rid)).get("mediaRooms", [])

    def logout(self):
        try:
            return self._call(paths.logout)
        finally:
            self._authkey = None

    # -- writes ------------------------------------------------------------------------

    def set_light(self, light_id, *, pct=None, raw=None, time_ms=0):
        """Set one light. Give `pct` (0–100) or `raw` (0–65535); `time_ms` is the fade."""
        level = raw if raw is not None else pct_to_raw(pct)
        if level is None:
            raise ValueError("give pct or raw")
        return self.set_lights([(light_id, level, time_ms)])

    def set_lights(self, items):
        """items: iterable of (id, raw_level 0-65535, time_ms)."""
        return self._call(paths.lights_set(list(items)))

    def set_shade(self, shade_id, *, pct=None, raw=None):
        pos = raw if raw is not None else pct_to_raw(pct)
        if pos is None:
            raise ValueError("give pct or raw")
        return self.set_shades([(shade_id, pos)])

    def set_shades(self, items):
        return self._call(paths.shades_set(list(items)))

    def recall_scene(self, scene_id):
        return self._call(paths.scene_recall(scene_id))

    def set_setpoint(self, thermostat_id, setpoints_c_or_f):
        """setpoints_c_or_f: iterable of (type in {Auto,Cool,Heat}, temperature in whole degrees).
        Temperature is converted to the tenths the processor wants."""
        deci = [(t, temp_to_deci(temp)) for (t, temp) in setpoints_c_or_f]
        return self._call(paths.thermostat_setpoint(thermostat_id, deci))

    def set_mode(self, items):
        """items: (id, mode in {HEAT,COOL,AUTO,OFF})."""
        return self._call(paths.thermostat_mode(list(items)))

    def set_fanmode(self, items):
        """items: (id, mode in {AUTO,ON})."""
        return self._call(paths.thermostat_fanmode(list(items)))

    def set_schedule(self, items):
        """items: (id, mode in {RUN,HOLD})."""
        return self._call(paths.thermostat_schedule(list(items)))

    def lock(self, lock_id):
        return self._call(paths.doorlock("lock", lock_id))

    def unlock(self, lock_id):
        return self._call(paths.doorlock("unlock", lock_id))

    # media rooms
    def mediaroom_mute(self, room_id):
        return self._call(paths.mediaroom(room_id, "mute"))

    def mediaroom_unmute(self, room_id):
        return self._call(paths.mediaroom(room_id, "unmute"))

    def mediaroom_select_source(self, room_id, source_id):
        return self._call(paths.mediaroom(room_id, "selectsource", source_id))

    def mediaroom_volume(self, room_id, pct):
        """Volume 0–100 percent (note: this is NOT the 0–65535 lights scale)."""
        return self._call(paths.mediaroom(room_id, "volume", max(0, min(100, int(pct)))))

    def mediaroom_power(self, room_id, on):
        return self._call(paths.mediaroom(room_id, "power", "on" if on else "off"))
