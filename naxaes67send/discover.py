"""Finding the amplifiers, so nobody types six addresses.

Measured on 110 Roosevelt (2026-09-14), which has six DM-NAX and an `amps: []`:

* A DM-NAX is a Crestron *peripheral*. Like the touch panels it connects out to
  the processor; it does not listen on CIP 41794, so the sweep that finds
  processors finds no amplifiers. What every Crestron device does serve is its
  web UI on 443, answering with `Server: Crestron Webserver` - which no camera,
  console or phone does. That narrows 82 hosts to 26 with no credentials.
* Reverse DNS then names most of them, `<MODEL>-<MAC>`: `TSW-880R-…`, `CP4-R-…`,
  `CEN-GW1-…`, `PC-350V-…`, `DM-NAX-8ZSA-…`. Half the amplifiers there had been
  renamed by an installer (`NAX-4-BBFA`), so "starts with DM-NAX" is not the
  rule; "has NAX in the name" is, and it is only a hint.
* The credentials are what make it certain: log in, and an amplifier answers
  `ZoneOutputs`, which nothing else does. The password is the house PIN; the
  user is not fixed - Crestron devices ship as `admin` on some firmware and
  `chdevice` on others (both amplifiers there said 403 to `admin`), so both
  are tried, configured one first, and the one that worked is what gets
  saved. A device with NAX in its name that lets neither in is reported as
  found-but-unverified, by name and address, so the person fixes the
  password instead of typing the address.

Verified amplifiers are added to the add-on's options through the Supervisor,
so they show on the Options page like ones typed by hand, and are usable by the
running add-on at once. Adds; never removes or rewrites what a person entered.
"""
import concurrent.futures
import json
import os
import socket
import ssl
import threading
import time
import urllib.error
import urllib.request

WEB_PORT = 443
SWEEP_THREADS = 48
TCP_TIMEOUT = 0.35
EVERY = 6 * 3600

# Crestron hostnames that are certainly not amplifiers. Anything else that
# serves the Crestron web UI is worth a closer look.
NOT_AMPS = ("TSW-", "TS-", "TST-", "TSS-", "TPMC", "CP4", "CP3", "MC4", "MC3", "DIN-",
            "CEN-", "PC-350", "DM-NVX", "DM-MD", "HD-", "AM-", "TSR-", "HR-", "ZUM", "CLW", "GLPP")

# The two usernames Crestron firmware ships with. The configured `amp_user`
# goes first; the other is the fallback.
USERS = ("admin", "chdevice")

# Amplifiers found and verified in this process, so `api.amplifiers()` sees them
# before the next restart re-reads the options.
DISCOVERED = {}
STATE = {"at": None, "base": None, "crestron": 0, "found": [], "added": [], "unverified": [],
         "skipped": [], "error": None, "running": False}
_LOCK = threading.Lock()


def _log(log, msg):
    (log or print)(f"[discover] {msg}")


# -- the network ------------------------------------------------------------------

def sweep(base, port=WEB_PORT):
    """Every host on `base`.0/24 with `port` open. ~4 s for 254 addresses."""
    def probe(i):
        target = f"{base}.{i}"
        c = socket.socket()
        c.settimeout(TCP_TIMEOUT)
        try:
            c.connect((target, port))
            return target
        except OSError:
            return None
        finally:
            c.close()
    with concurrent.futures.ThreadPoolExecutor(SWEEP_THREADS) as ex:
        return [h for h in ex.map(probe, range(1, 255)) if h]


def _lax():
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    return ctx


def crestron_web(host, timeout=4):
    """True if the web UI on 443 is Crestron's. Reads the `Server` header only."""
    req = urllib.request.Request(f"https://{host}/", headers={
        "Origin": f"https://{host}", "User-Agent": "NaxAes67Sender discover"})
    try:
        with urllib.request.urlopen(req, timeout=timeout, context=_lax()) as r:
            return "crestron" in (r.headers.get("Server") or "").lower()
    except urllib.error.HTTPError as e:
        return "crestron" in (e.headers.get("Server") or "").lower()
    except Exception:
        return False


def rdns(host):
    try:
        name = socket.gethostbyaddr(host)[0]
        return name.split(".", 1)[0]
    except OSError:
        return ""


def classify(name):
    """'amp' | 'other' | 'unknown' from a Crestron hostname."""
    up = (name or "").upper()
    if not up:
        return "unknown"
    if "NAX" in up:
        return "amp"
    if up.startswith(NOT_AMPS):
        return "other"
    return "unknown"


def users_to_try(user):
    first = (user or "admin").strip()
    return [first] + [u for u in USERS if u != first]


def verify(host, user, password, log=None):
    """Log in and prove it is an amplifier: only a DM-NAX answers ZoneOutputs.

    Tries `user`, then the other factory username. The result says which one
    the amplifier took, so that is what gets saved with it.

    Imported here rather than at the top so this module stays standard-library
    for the things that do not need an amplifier (and for the tests).
    """
    import naxctl
    errors = []
    nax = None
    for u in users_to_try(user):
        try:
            nax = naxctl.Nax(host, u, password, log=lambda *a, **k: None)
            nax.login()
            user = u
            break
        except Exception as e:
            errors.append(f"{u}: {type(e).__name__}: {str(e).split('.')[0]}")
            nax = None
    if nax is None:
        return {"ok": False, "error": "; ".join(errors)}
    try:
        zones = nax.zones()
    except Exception as e:
        return {"ok": False, "error": f"{user}: logged in, but {type(e).__name__}: {e}"}
    model = ""
    try:
        model = str((nax.get("DeviceInfo").get("DeviceInfo") or {}).get("Model") or "")
    except Exception:
        pass
    try:
        nax.close()
    except Exception:
        pass
    if not zones:
        return {"ok": False, "error": "logged in, but it has no zones - not an amplifier"}
    return {"ok": True, "user": user, "model": model, "zones": len(zones),
            "zone_names": [v.get("name") for v in zones.values() if v.get("name")][:12]}


# -- one pass ---------------------------------------------------------------------

def scan(base, user, password, known, log=None, subnet=None):
    """One pass. Returns the report; touches nothing.

    `known` is {host: ...} of amplifiers already configured - those are never
    re-verified or re-added, and their passwords are never touched.
    """
    base = ".".join((subnet or base).split(".")[:3])
    report = {"at": time.time(), "base": base, "crestron": 0, "found": [], "unverified": [],
              "skipped": [], "error": None}
    hits = sweep(base)
    for host in hits:
        if host in known:
            continue
        if not crestron_web(host):
            continue
        report["crestron"] += 1
        name = rdns(host)
        kind = classify(name)
        if kind == "other":
            report["skipped"].append({"host": host, "name": name})
            continue
        # An amplifier by name, or a Crestron device we cannot name: the
        # credentials decide. Only these cost a login.
        if not password:
            entry = {"host": host, "name": name, "error": "no amp_password set"}
            (report["unverified"] if kind == "amp" else report["skipped"]).append(entry)
            continue
        v = verify(host, user, password, log=log)
        if v.get("ok"):
            report["found"].append({"host": host, "name": name, "user": v.get("user") or user,
                                    "model": v.get("model"), "zones": v.get("zones"),
                                    "zone_names": v.get("zone_names")})
        elif kind == "amp":
            report["unverified"].append({"host": host, "name": name, "error": v.get("error")})
        else:
            report["skipped"].append({"host": host, "name": name, "error": v.get("error")})
    _log(log, f"{base}.0/24: {len(hits)} on 443, {report['crestron']} Crestron, "
              f"{len(report['found'])} amplifier(s) verified, {len(report['unverified'])} unverified")
    for u in report["unverified"]:
        _log(log, f"{u['name'] or u['host']} at {u['host']} looks like an amplifier but "
                  f"refused the login ({u.get('error')}) - check amp_user / amp_password")
    return report


# -- writing what was found ---------------------------------------------------------

# On the host's network the `supervisor` name does not resolve; the Supervisor's
# fixed address on its own network is the fallback. Same as the Rava bridge.
SUPERVISOR_URLS = ("http://supervisor", "http://172.30.32.2")


def _supervisor(path, token, body=None):
    last = None
    for base in SUPERVISOR_URLS:
        req = urllib.request.Request(base + path, data=body, method="POST" if body is not None else "GET",
                                     headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=10) as r:
                return json.load(r)
        except urllib.error.HTTPError as e:
            raise RuntimeError(f"{e.code} {e.read()[:200].decode('utf-8', 'replace')}") from e
        except Exception as e:
            last = e
    raise RuntimeError(f"Supervisor not reachable: {last!r}")


def apply(found, user, password, log=None):
    """Add verified amplifiers to the options. Adds only; never edits a saved one."""
    added = []
    if not found:
        return added
    token = os.environ.get("SUPERVISOR_TOKEN") or os.environ.get("HASSIO_TOKEN")
    for f in found:
        DISCOVERED.setdefault(f["host"], {"user": f.get("user") or user or "admin", "password": password})
    if not token:
        _log(log, "no SUPERVISOR_TOKEN - the amplifiers are usable until the next restart only")
        return [f["host"] for f in found]
    options = _supervisor("/addons/self/info", token)["data"]["options"]
    amps = list(options.get("amps") or [])
    have = {str(a.get("host") or "").strip() for a in amps}
    for f in found:
        if f["host"] not in have:
            amps.append({"host": f["host"], "user": f.get("user") or user or "admin", "password": password})
            added.append(f["host"])
    if added:
        options["amps"] = amps
        _supervisor("/addons/self/options", token, json.dumps({"options": options}).encode())
        _log(log, "added to the options: " + ", ".join(added))
    return added


def run(base, user, password, known, log=None, subnet=None):
    """A scan and, for anything verified, the save. Never raises; the report says."""
    with _LOCK:
        if STATE["running"]:
            return dict(STATE, error="a scan is already running")
        STATE["running"] = True
    try:
        report = scan(base, user, password, known, log=log, subnet=subnet)
        try:
            report["added"] = apply(report["found"], user, password, log=log)
        except Exception as e:
            report["added"] = []
            report["error"] = f"found {len(report['found'])} but could not save: {e}"
            _log(log, report["error"])
    except Exception as e:
        report = dict(STATE, error=f"{type(e).__name__}: {e}", at=time.time())
        _log(log, report["error"])
    finally:
        STATE["running"] = False
    STATE.update({k: report.get(k) for k in ("at", "base", "crestron", "found", "added",
                                             "unverified", "skipped", "error")})
    return report


def loop(base_fn, user, password, known_fn, log=None, subnet=None, every=EVERY, first_delay=15):
    """At start, then every six hours. `base_fn`/`known_fn` are called each time,
    because the address and the configured amplifiers can both change."""
    time.sleep(first_delay)
    while True:
        try:
            run(base_fn(), user, password, known_fn(), log=log, subnet=subnet)
        except Exception as e:
            _log(log, f"scan failed: {e!r}")
        time.sleep(every)


def public():
    """What /health shows."""
    return {k: STATE.get(k) for k in ("at", "base", "crestron", "found", "added",
                                      "unverified", "skipped", "error", "running")}
