"""Finding the panels and the doors, so nobody types an IP or a ten-digit extension.

Panels: one SIP OPTIONS to every host on the bridge's own /24. Only SIP stacks
answer, and a Crestron panel signs its answer with its hostname in User-Agent
(`TSW-770R-C442683542EB`), which is also the model and the MAC. That finds
exactly the set of things this bridge can ring, with no credentials at all.
Then, if the panel console password is known, an SSH session runs `SIPINFO`
and reads the two things the SIP layer does not reveal: the panel's own
extension, and its page groups - which are Crestron Home's room groups
(`RG_1ST_FLOOR_53102`), so a door can ring "1st floor" by Crestron Home's own
definition of it.

Doors: the UniFi Access developer API lists every reader; the ones that
advertise `support_third_party_sip` are doors this bridge can serve. Each
becomes an account named after the reader's alias ("Side Door" -> `sidedoor`)
with the house PIN as its password, plus the door id for `/unlock`. The one
step this cannot do is the reader's own SIP settings: Ubiquiti exposes those
only in the Access app, so `/discover` reports what to type there.

What is found is merged into the add-on's options through the Supervisor, so
it shows up in the Options page like anything typed by hand, and is applied to
the running bridge without a restart. Nothing a person entered is removed or
renamed; discovery only adds, and refreshes the IP of a panel it already knows.
"""
import json
import os
import random
import re
import secrets
import socket
import ssl
import threading
import time
import urllib.error
import urllib.request

PANEL_PREFIXES = ("TSW-", "TS-", "TST-", "TSS-", "TPMC", "CRESTRON")
DEFAULT_PIN = "2129918115"


def _log(log, msg):
    (log or print)(f"discover: {msg}")


# -- panels -------------------------------------------------------------------

def scan_panels(bind_ip, subnet=None, wait=3.0, log=None):
    """SIP OPTIONS to every host on the /24. Returns [{host, hostname, model, mac}]."""
    base = ".".join((subnet or bind_ip).split(".")[:3])
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.bind(("0.0.0.0", 0))
    s.settimeout(0.2)
    port = s.getsockname()[1]
    rnd = lambda n=8: secrets.token_hex(n // 2)
    for i in range(1, 255):
        host = f"{base}.{i}"
        if host == bind_ip:
            continue
        msg = (f"OPTIONS sip:CRESTRON@{host}:5060 SIP/2.0\r\n"
               f"Via: SIP/2.0/UDP {bind_ip}:{port};branch=z9hG4bK{rnd()};rport\r\n"
               f"Max-Forwards: 70\r\nFrom: <sip:discover@{bind_ip}:{port}>;tag={rnd()}\r\n"
               f"To: <sip:CRESTRON@{host}>\r\nCall-ID: {rnd(12)}@{bind_ip}\r\nCSeq: 1 OPTIONS\r\n"
               f"Contact: <sip:discover@{bind_ip}:{port}>\r\nUser-Agent: RavaBridge discover\r\n"
               f"Accept: application/sdp\r\nContent-Length: 0\r\n\r\n")
        try:
            s.sendto(msg.encode(), (host, 5060))
        except OSError:
            pass
    found = {}
    end = time.time() + wait
    while time.time() < end:
        try:
            data, addr = s.recvfrom(65535)
        except socket.timeout:
            continue
        except OSError:
            break
        txt = data.decode("utf-8", "replace")
        if not txt.startswith("SIP/2.0 200"):
            continue
        m = re.search(r"^(?:User-Agent|Server):\s*(.*)$", txt, re.M | re.I)
        ua = m.group(1).strip() if m else ""
        if not ua.upper().startswith(PANEL_PREFIXES):
            continue
        model, _, mac = ua.rpartition("-")
        found[addr[0]] = {"host": addr[0], "hostname": ua, "model": model or ua,
                          "mac": mac.lower() if re.fullmatch(r"[0-9A-Fa-f]{12}", mac) else ""}
    s.close()
    _log(log, f"{len(found)} panel(s) answered SIP OPTIONS on {base}.0/24")
    return [found[k] for k in sorted(found, key=lambda ip: int(ip.split(".")[-1]))]


def panel_details(host, user="admin", password=DEFAULT_PIN, timeout=12, log=None):
    """`SIPINFO` over SSH: the panel's extension and its page groups.

    Needs paramiko, which the add-on image carries; without it, or without the
    password, a panel is still usable - the bridge falls back to `CRESTRON` as
    the extension, which the panel was measured to accept for OPTIONS.
    """
    try:
        import paramiko
    except ImportError:
        return None
    try:
        c = paramiko.SSHClient()
        c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        c.connect(host, username=user, password=password, timeout=timeout,
                  look_for_keys=False, allow_agent=False, banner_timeout=timeout)
        sh = c.invoke_shell(width=200)
        time.sleep(1.0)
        sh.send("SIPINFO\r")
        out, last, start = b"", time.time(), time.time()
        while time.time() - start < timeout:
            if sh.recv_ready():
                out += sh.recv(65535)
                last = time.time()
            elif time.time() - last > 1.0 and b"page group" in out.lower():
                break
            else:
                time.sleep(0.1)
        c.close()
    except Exception as e:
        _log(log, f"{host}: SIPINFO over SSH failed: {e!r}")
        return None
    txt = out.decode("utf-8", "replace")
    ext = re.search(r"SIP local ext:\s*(\S+)", txt)
    groups = re.search(r"SIP page group\(s\):\s*(.*)", txt)
    mode = re.search(r"SIP connection mode:\s*(\S+)", txt)
    return {"ext": ext.group(1).strip() if ext else "",
            "groups": [g.strip() for g in (groups.group(1) if groups else "").split(",") if g.strip()],
            "mode": mode.group(1).strip() if mode else ""}


def pretty_group(raw):
    """`RG_1ST_FLOOR_53102` -> `1st floor`; `CRESTRON` stays as it is."""
    m = re.fullmatch(r"RG_(.+?)_\d+", raw)
    return m.group(1).replace("_", " ").lower() if m else raw.lower()


# -- doors ----------------------------------------------------------------------

def access_get(host, token, path, timeout=10):
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    req = urllib.request.Request(f"https://{host}:12445/api/v1/developer{path}",
                                 headers={"Authorization": f"Bearer {token}", "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout, context=ctx) as r:
        return json.load(r)


def scan_doors(host, token, log=None):
    """Every UniFi Access reader that can do third-party SIP, as a door entry."""
    try:
        devs = access_get(host, token, "/devices")
        doors = access_get(host, token, "/doors")
    except Exception as e:
        _log(log, f"UniFi Access at {host}: {e!r}")
        return []
    flat = []
    for grp in devs.get("data") or []:
        flat.extend(grp if isinstance(grp, list) else [grp])
    by_id = {d.get("id"): d for d in (doors.get("data") or []) if isinstance(d, dict)}
    out = []
    for it in flat:
        if not isinstance(it, dict) or "support_third_party_sip" not in (it.get("capabilities") or []):
            continue
        door = by_id.get(it.get("location_id"), {})
        alias = it.get("alias") or door.get("name") or it.get("name") or it.get("id")
        user = re.sub(r"[^a-z0-9]", "", str(alias).lower()) or str(it.get("id"))
        out.append({"name": str(alias), "user": user, "reader": it.get("type"), "reader_id": it.get("id"),
                    "door_id": it.get("location_id") or "", "door": door.get("name"),
                    "online": bool(it.get("is_online"))})
    _log(log, f"{len(out)} SIP-capable reader(s) at {host}: " + ", ".join(f"{d['name']} ({d['reader']})" for d in out))
    return out


# -- merge and persist ------------------------------------------------------------

def merge(options, panels, details, doors, pin):
    """New findings into the options dict. Adds; never removes or renames."""
    changed = []
    have = {p.get("host"): p for p in options.get("panels") or []}
    by_mac = {}
    for p in options.get("panels") or []:
        m = re.search(r"([0-9a-f]{12})$", (p.get("hostname") or "").lower())
        if m:
            by_mac[m.group(1)] = p
    for f in panels:
        d = details.get(f["host"]) or {}
        groups = [pretty_group(g) for g in d.get("groups") or [] if g.upper() != "CRESTRON"]
        existing = have.get(f["host"]) or (by_mac.get(f["mac"]) if f["mac"] else None)
        if existing is None:
            options.setdefault("panels", []).append({
                "name": f["hostname"], "host": f["host"], "ext": d.get("ext") or "CRESTRON",
                "groups": groups, "hostname": f["hostname"]})
            changed.append(f"panel {f['hostname']} at {f['host']}" + (f" ext {d['ext']}" if d.get("ext") else ""))
        else:
            before = json.dumps(existing, sort_keys=True)
            if existing.get("host") != f["host"]:
                existing["host"] = f["host"]
            if d.get("ext") and not existing.get("ext"):
                existing["ext"] = d["ext"]
            if groups and not existing.get("groups"):
                existing["groups"] = groups
            existing.setdefault("hostname", f["hostname"])
            if json.dumps(existing, sort_keys=True) != before:
                changed.append(f"panel {existing.get('name')} refreshed")
    users = {d.get("user") for d in options.get("doors") or []}
    for f in doors:
        if f["user"] in users:
            for d in options["doors"]:
                if d.get("user") == f["user"] and not d.get("door_id") and f.get("door_id"):
                    d["door_id"] = f["door_id"]
                    changed.append(f"door {d.get('name')} learned its Access door id")
            continue
        options.setdefault("doors", []).append({
            "name": f["name"], "user": f["user"], "password": pin, "host": "", "ring": ["all"],
            "door_id": f.get("door_id") or ""})
        changed.append(f"door {f['name']} as {f['user']}")
    return changed


# An add-on on the host's network cannot resolve the `supervisor` name - that
# name lives on the Supervisor's own Docker network - so the fixed address of
# the Supervisor on that network is the fallback.
SUPERVISOR_URLS = ("http://supervisor", "http://172.30.32.2")
SUPERVISOR_STATE = {"token": False, "url": None, "error": None}


def _supervisor(path, token, body=None):
    last = None
    for base in ([SUPERVISOR_STATE["url"]] if SUPERVISOR_STATE["url"] else []) + [u for u in SUPERVISOR_URLS if u != SUPERVISOR_STATE["url"]]:
        req = urllib.request.Request(base + path, data=body, method="POST" if body is not None else "GET",
                                     headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=8) as r:
                SUPERVISOR_STATE["url"] = base
                SUPERVISOR_STATE["error"] = None
                return json.load(r)
        except urllib.error.HTTPError as e:
            # Reached it; it said no. Do not try another address.
            SUPERVISOR_STATE["url"] = base
            SUPERVISOR_STATE["error"] = f"{e.code} {e.read()[:200].decode('utf-8', 'replace')}"
            raise
        except Exception as e:
            last = e
    SUPERVISOR_STATE["error"] = repr(last)
    raise last


def supervisor_options():
    token = os.environ.get("SUPERVISOR_TOKEN") or os.environ.get("HASSIO_TOKEN")
    SUPERVISOR_STATE["token"] = bool(token)
    if not token:
        return None, None
    return _supervisor("/addons/self/info", token)["data"]["options"], token


def supervisor_write(options, token):
    return _supervisor("/addons/self/options", token, body=json.dumps({"options": options}).encode())


LOCAL_STATE = "/data/discovered.json"


def save_local(options, path=LOCAL_STATE):
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump({"panels": options.get("panels") or [], "doors": options.get("doors") or [],
                       "at": time.time()}, f)
    except OSError:
        pass


def load_local(cfg, path=LOCAL_STATE):
    """Options plus what an earlier discovery found: union by panel host and door user."""
    try:
        with open(path, encoding="utf-8") as f:
            saved = json.load(f)
    except (OSError, ValueError):
        return cfg
    hosts = {p.get("host") for p in cfg.get("panels") or []}
    for p in saved.get("panels") or []:
        if p.get("host") and p["host"] not in hosts:
            cfg.setdefault("panels", []).append(p)
    users = {d.get("user") for d in cfg.get("doors") or []}
    for d in saved.get("doors") or []:
        if d.get("user") and d["user"] not in users:
            cfg.setdefault("doors", []).append(d)
        elif d.get("user") in users and d.get("door_id"):
            for mine in cfg["doors"]:
                if mine.get("user") == d["user"] and not mine.get("door_id"):
                    mine["door_id"] = d["door_id"]
    return cfg


def run(cfg, bind_ip, log=None, apply=None):
    """One discovery pass. Returns a report; `apply(options)` is called with the merged options."""
    report = {"panels": [], "doors": [], "changed": [], "readerSetup": None, "error": None}
    pin = str(cfg.get("panel_password") or DEFAULT_PIN)
    panels = scan_panels(bind_ip, cfg.get("subnet") or None, log=log)
    details = {}
    for p in panels:
        d = panel_details(p["host"], cfg.get("panel_user") or "admin", pin, log=log)
        if d:
            details[p["host"]] = d
        report["panels"].append({**p, **(d or {})})
    acc = cfg.get("access") or {}
    doors = []
    if acc.get("host") and acc.get("token"):
        doors = scan_doors(acc["host"], acc["token"], log=log)
        report["doors"] = doors
        report["readerSetup"] = {
            "where": "Access app > Interface Designer > reader > Doorbell Call > Third-Party SIP > Configure",
            "server": bind_ip, "port": int(cfg.get("sip_port") or 5060), "transport": "UDP",
            "accounts": [{"reader": d["name"], "user": d["user"], "password": str(cfg.get("door_password") or pin)} for d in doors],
        }
    try:
        options, token = supervisor_options()
        if options is None:
            report["error"] = "no SUPERVISOR_TOKEN in the environment - findings apply to this run only"
    except Exception as e:
        options, token = None, None
        report["error"] = f"supervisor: {e!r}"
    live = options if options is not None else json.loads(json.dumps(cfg))
    report["changed"] = merge(live, panels, details, doors, str(cfg.get("door_password") or pin))
    if report["changed"]:
        if token:
            try:
                supervisor_write(live, token)
                report["saved"] = True
            except Exception as e:
                body = getattr(e, "read", lambda: b"")()
                report["error"] = f"could not save options: {e!r} {body[:300].decode('utf-8', 'replace') if body else ''}"
        if not report.get("saved"):
            # The Options page cannot be updated, so remember the findings here;
            # `main.load()` folds this in at start. The house still rings after a
            # restart, which is the part that matters.
            save_local(live)
        if apply:
            apply(live)
        _log(log, "; ".join(report["changed"]) + (" (saved to the options)" if report.get("saved") else ""))
    else:
        _log(log, "nothing new")
    if report["error"]:
        _log(log, report["error"])
    return report


if __name__ == "__main__":
    # python discover.py <bind_ip> [access_host access_token] [panel_password]
    import sys
    cfg = {"access": {}}
    if len(sys.argv) >= 4:
        cfg["access"] = {"host": sys.argv[2], "token": sys.argv[3]}
    if len(sys.argv) >= 5:
        cfg["panel_password"] = sys.argv[4]
    out = run(cfg, sys.argv[1])
    print(json.dumps(out, indent=1))
