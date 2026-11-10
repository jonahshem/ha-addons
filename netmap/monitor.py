#!/usr/bin/env python3
"""
BAV NetMap add-on: netscan.py on a schedule, plus an up/down monitor for the devices that matter.

  scans      netscan.py into /share/netmap/plan every `scan_interval_hours` (and on the button).
             That folder IS the plan: edit devices.csv / networks.csv there over Samba and the next
             scan keeps the edits; the laptop's unifi_import.py reads the same files.
             Old IP sheets dropped into /share/netmap/known/ are passed as --known.
  monitor    every device with reserve=Y in devices.csv, by ping. A device that ignores ping is
             checked by a TCP connect to a port the scan found open (web/ssh/Crestron CIP/RTSP
             only - never Modbus or CoolMaster, see netscan.PORTS). Devices in `fast_watch` are
             checked every 5 s, so a reboot of a few seconds shows up as a "blip".
  Home Assistant
             binary_sensor.netmap_<name> (connectivity) per device and sensor.netmap_offline;
             events netmap_device_offline / netmap_device_online / netmap_new_device; a
             notification once a device has been down `notify_after_seconds`, and again when it
             is back. States are re-posted every 10 minutes so they survive a Core restart.
  page       the add-on's sidebar panel (ingress only): device status, recent events, the map.
"""
from __future__ import annotations

import collections
import concurrent.futures as cf
import html
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import netscan  # noqa: E402

DATA = os.environ.get("NETMAP_DATA", "/data")
SHARE = os.environ.get("NETMAP_SHARE", "/share/netmap")
PLAN = os.path.join(SHARE, "plan")
KNOWN = os.path.join(SHARE, "known")
INGRESS_PORT = 8099
# Only these may open the page: the Supervisor's ingress proxy, and this box. host_network
# puts the port on the LAN too, and the page has a scan button.
INGRESS_PEERS = {"172.30.32.2", "127.0.0.1"}
SAFE_TCP = [80, 443, 22, 41794, 554, 8080, 8443, 1400, 9100]  # for devices that ignore ping
FAST_INTERVAL = 5
STATE_REFRESH = 600
SUPERVISOR = "http://supervisor"

LOCK = threading.RLock()


def log(msg: str) -> None:
    print(time.strftime("%Y-%m-%d %H:%M:%S ") + msg, flush=True)


def local_time(t: float | None) -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(t)) if t else ""


def human(seconds: float) -> str:
    s = int(seconds)
    if s < 90:
        return f"{s} s"
    if s < 5400:
        return f"{s // 60} min"
    return f"{s // 3600} h {s % 3600 // 60:02d} min"


def load_options() -> dict:
    defaults = {"site": "house", "subnets": [], "scan_interval_hours": 6, "monitor_interval_seconds": 60,
                "down_after_checks": 3, "notify_after_seconds": 180, "notify_service": "",
                "notify_new_devices": True, "fast_watch": [], "gentle": False}
    try:
        with open(os.path.join(DATA, "options.json")) as f:
            defaults.update(json.load(f))
    except (OSError, ValueError):
        pass
    return defaults


# --------------------------------------------------------------------------- Home Assistant

class HA:
    def __init__(self, token: str | None):
        self.token = token

    def call(self, method: str, path: str, body=None):
        if not self.token:
            return None
        req = urllib.request.Request(SUPERVISOR + "/core/api" + path, method=method,
                                     data=json.dumps(body).encode() if body is not None else None,
                                     headers={"Authorization": f"Bearer {self.token}",
                                              "Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=10) as r:
                return r.status
        except (urllib.error.URLError, OSError) as e:
            log(f"HA {method} {path}: {e}")
            return None

    def state(self, entity: str, state, attrs: dict) -> None:
        self.call("POST", f"/states/{entity}", {"state": state, "attributes": attrs})

    def event(self, kind: str, data: dict) -> None:
        self.call("POST", f"/events/{kind}", data)

    def notify(self, service: str, title: str, message: str) -> None:
        if service:
            svc = service.split(".", 1)[-1] if service.startswith("notify.") else service
            if self.call("POST", f"/services/notify/{svc}", {"title": title, "message": message}):
                return
        self.call("POST", "/services/persistent_notification/create",
                  {"title": title, "message": message, "notification_id": f"netmap_{int(time.time())}"})


# --------------------------------------------------------------------------- checks

def ping(ip: str) -> bool:
    exe = shutil.which("ping")
    if not exe:
        return False
    try:
        p = subprocess.run([exe, "-c", "1", "-W", "1", "-n", "-q", ip], capture_output=True, timeout=4)
        return p.returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def tcp_alive(ip: str, port: int) -> bool:
    return netscan.tcp_state(ip, port, 1.5) in ("open", "refused")


class Device:
    def __init__(self, key: str, name: str, ip: str, mac: str, role: str, ports: list[int], fast: bool):
        self.key, self.name, self.ip, self.mac, self.role = key, name, ip, mac, role
        self.ports, self.fast = ports, fast
        self.method = "ping"          # switches to tcp/<port> if ping never answers
        self.up: bool | None = None   # None until the first verdict
        self.fails = 0
        self.down_since: float | None = None
        self.notified = False
        self.last_seen: float | None = None
        self.blips: collections.deque = collections.deque()  # start times of short outages, 24 h
        self.next_due = 0.0
        self.busy = False

    def entity(self) -> str:
        slug = re.sub(r"[^a-z0-9]+", "_", self.name.lower()).strip("_") or "device"
        return f"binary_sensor.netmap_{slug}_{self.ip.split('.')[-1]}"

    def check(self) -> bool:
        if self.method == "ping":
            if ping(self.ip):
                return True
            for p in [p for p in SAFE_TCP if p in self.ports]:
                if tcp_alive(self.ip, p):
                    self.method = f"tcp/{p}"  # answers TCP but not ping: use that from now on
                    return True
            return False
        return tcp_alive(self.ip, int(self.method.split("/")[1])) or ping(self.ip)


# --------------------------------------------------------------------------- the service

class Service:
    def __init__(self, opts: dict, ha: HA):
        self.o, self.ha = opts, ha
        self.devices: dict[str, Device] = {}
        self.events: collections.deque = collections.deque(maxlen=500)
        self.scan_now = threading.Event()
        self.scanning = False
        self.last_scan = 0.0
        self.last_refresh = 0.0
        self.state = {"known_macs": [], "last_scan": 0}
        self._load()

    # ---- persistence
    def _load(self) -> None:
        try:
            with open(os.path.join(DATA, "state.json")) as f:
                self.state.update(json.load(f))
        except (OSError, ValueError):
            pass
        self.last_scan = float(self.state.get("last_scan") or 0)
        try:
            with open(os.path.join(DATA, "events.jsonl")) as f:
                for line in f.readlines()[-500:]:
                    self.events.append(json.loads(line))
        except (OSError, ValueError):
            pass

    def _save(self) -> None:
        with LOCK:
            tmp = os.path.join(DATA, "state.json.tmp")
            with open(tmp, "w") as f:
                json.dump(self.state, f)
            os.replace(tmp, os.path.join(DATA, "state.json"))

    def event(self, kind: str, text: str, dev: Device | None = None, **extra) -> None:
        e = {"t": time.time(), "when": local_time(time.time()), "kind": kind, "text": text, **extra}
        if dev:
            e.update(name=dev.name, ip=dev.ip, mac=dev.mac)
        with LOCK:
            self.events.append(e)
            with open(os.path.join(DATA, "events.jsonl"), "a") as f:
                f.write(json.dumps(e) + "\n")
        log(f"{kind}: {text}")

    # ---- watch list from the plan
    def load_watch(self) -> None:
        path = os.path.join(PLAN, "devices.csv")
        rows = netscan.read_csv(path)
        fast = {str(x).strip() for x in self.o.get("fast_watch") or []}
        new: dict[str, Device] = {}
        for r in rows:
            if not (r.get("reserve") or "").strip().upper().startswith("Y") and r.get("ip") not in fast:
                continue
            ip = (r.get("fixed_ip") or r.get("ip") or "").strip()
            key = r.get("mac") or ip
            ports = [int(x.split("/")[0]) for x in (r.get("open_ports") or "").split() if x.split("/")[0].isdigit()]
            name = r.get("unifi_name") or r.get("best_name") or ip
            old = self.devices.get(key)
            d = Device(key, name, ip, r.get("mac", ""), r.get("role", ""), ports, ip in fast or r.get("ip") in fast)
            if old:  # keep the running state across a reload
                for a in ("method", "up", "fails", "down_since", "notified", "last_seen", "blips", "next_due"):
                    setattr(d, a, getattr(old, a))
                if old.ip != d.ip:
                    d.method = "ping"
            new[key] = d
        with LOCK:
            self.devices = new
        log(f"watching {len(new)} devices ({sum(d.fast for d in new.values())} fast)")

    # ---- scanning
    def run_scan(self) -> None:
        self.scanning = True
        try:
            os.makedirs(PLAN, exist_ok=True)
            os.makedirs(KNOWN, exist_ok=True)
            args = ["--site", self.o["site"], "--out", PLAN, "--oui", os.path.join(DATA, "oui.csv")]
            for s in self.o.get("subnets") or []:
                args += ["--subnet", s]
            for f in sorted(os.listdir(KNOWN)):
                if f.lower().endswith((".txt", ".csv")):
                    args += ["--known", os.path.join(KNOWN, f)]
            if self.o.get("gentle"):
                args.append("--gentle")
            log("scan starting: netscan " + " ".join(args))
            rc = netscan.main(args)
            if rc != 0:
                self.event("scan", f"scan failed (exit {rc}); see the add-on log")
                return
            self.after_scan()
        except Exception as e:  # noqa: BLE001 - keep the monitor alive whatever a scan does
            self.event("scan", f"scan crashed: {e!r}")
        finally:
            self.last_scan = time.time()
            self.state["last_scan"] = self.last_scan
            self._save()
            self.scanning = False

    def after_scan(self) -> None:
        try:
            with open(os.path.join(PLAN, "scan.json")) as f:
                scan = json.load(f)
        except (OSError, ValueError):
            return
        devs = [d for d in scan.get("devices", []) if d.get("mac") and not d.get("self")]
        known = set(self.state.get("known_macs") or [])
        first = not known
        # phones on private MACs come and go all day: never "new"
        fresh = [d for d in devs if d["mac"] not in known and not netscan.is_random_mac(d["mac"])]
        self.state["known_macs"] = sorted(known | {d["mac"] for d in devs})
        self.event("scan", f"scan done: {len(scan.get('devices', []))} devices, "
                           f"{len(fresh) if not first else 0} new, {scan.get('seconds')} s")
        if not first:
            for d in fresh:
                text = f"new device {d.get('best_name')} at {d['ip']} ({d.get('vendor') or 'unknown vendor'}, {d['mac']})"
                self.event("new", text, ip=d["ip"], mac=d["mac"], name=d.get("best_name"))
                self.ha.event("netmap_new_device", {"ip": d["ip"], "mac": d["mac"], "name": d.get("best_name"),
                                                    "vendor": d.get("vendor"), "role": d.get("role")})
            if fresh and self.o.get("notify_new_devices"):
                self.ha.notify(self.o.get("notify_service", ""), f"NetMap {self.o['site']}: new devices",
                               "\n".join(f"{d.get('best_name')} {d['ip']} ({d.get('vendor') or '?'})" for d in fresh[:20]))
        with LOCK:
            watched = {d.mac: d for d in self.devices.values() if d.mac}
        for d in devs:
            w = watched.get(d["mac"])
            if w and d["ip"] != w.ip:
                self.event("moved", f"{w.name} ({d['mac']}) is at {d['ip']}, the plan says {w.ip}", w)
        self.load_watch()
        self.last_refresh = 0  # publish the (maybe new) entity list at once

    def scan_loop(self) -> None:
        while True:
            due = self.last_scan + float(self.o["scan_interval_hours"]) * 3600
            if self.scan_now.is_set() or time.time() >= due:
                self.scan_now.clear()
                self.run_scan()
            self.scan_now.wait(timeout=30)

    # ---- monitoring
    def verdict(self, d: Device, alive: bool) -> list[str]:
        """Apply one check result; return notification lines."""
        now, notes = time.time(), []
        down_after = 2 if d.fast else max(1, int(self.o["down_after_checks"]))
        if alive:
            d.last_seen, d.fails = now, 0
            if d.up is False:
                took = now - (d.down_since or now)
                if took < float(self.o["notify_after_seconds"]):
                    d.blips.append(d.down_since or now)
                self.event("online", f"{d.name} ({d.ip}) is back after {human(took)}", d, seconds=round(took))
                self.ha.event("netmap_device_online", {"name": d.name, "ip": d.ip, "mac": d.mac,
                                                       "seconds_down": round(took)})
                if d.notified:
                    notes.append(f"back: {d.name} ({d.ip}) after {human(took)}")
            elif d.up is None:
                pass
            d.up, d.down_since, d.notified = True, None, False
        else:
            d.fails += 1
            if d.fails >= down_after and d.up is not False:
                interval = FAST_INTERVAL if d.fast else float(self.o["monitor_interval_seconds"])
                d.down_since = now - (d.fails - 1) * interval if d.up else now
                was = d.up
                d.up = False
                if was is not None:  # don't call a device "gone" on the very first look
                    self.event("offline", f"{d.name} ({d.ip}) stopped answering", d)
                    self.ha.event("netmap_device_offline", {"name": d.name, "ip": d.ip, "mac": d.mac})
                else:
                    self.event("offline", f"{d.name} ({d.ip}) did not answer since the monitor started", d)
            if d.up is False and not d.notified and d.down_since \
                    and now - d.down_since >= float(self.o["notify_after_seconds"]):
                d.notified = True
                notes.append(f"offline: {d.name} ({d.ip}) since {local_time(d.down_since)}")
        while d.blips and d.blips[0] < now - 86400:
            d.blips.popleft()
        return notes

    def publish(self, only: list[Device] | None = None) -> None:
        with LOCK:
            devs = list(self.devices.values())
        for d in (only if only is not None else devs):
            if d.up is None:
                continue
            self.ha.state(d.entity(), "on" if d.up else "off", {
                "friendly_name": f"{d.name}", "device_class": "connectivity", "icon": "mdi:lan-connect",
                "ip": d.ip, "mac": d.mac, "role": d.role, "checked_by": d.method,
                "last_seen": local_time(d.last_seen), "down_since": local_time(d.down_since),
                "blips_24h": len(d.blips), "source": "BAV NetMap"})
        down = [d for d in devs if d.up is False]
        self.ha.state("sensor.netmap_offline", len(down), {
            "friendly_name": f"NetMap {self.o['site']} offline", "unit_of_measurement": "devices",
            "icon": "mdi:lan-disconnect", "watched": len(devs),
            "offline": [f"{d.name} ({d.ip})" for d in down], "last_scan": local_time(self.last_scan)})

    def monitor_loop(self) -> None:
        pool = cf.ThreadPoolExecutor(32)
        pending: dict[cf.Future, Device] = {}
        while True:
            now = time.time()
            with LOCK:
                devs = list(self.devices.values())
            for d in devs:
                if not d.busy and now >= d.next_due:
                    d.busy = True
                    d.next_due = now + (FAST_INTERVAL if d.fast else float(self.o["monitor_interval_seconds"]))
                    pending[pool.submit(d.check)] = d
            changed, notes = [], []
            for f in [f for f in pending if f.done()]:
                d = pending.pop(f)
                d.busy = False
                before = d.up
                try:
                    alive = f.result()
                except Exception:  # noqa: BLE001
                    alive = False
                notes += self.verdict(d, alive)
                if d.up != before:
                    changed.append(d)
            if notes:
                self.ha.notify(self.o.get("notify_service", ""), f"NetMap {self.o['site']}", "\n".join(notes))
            if time.time() - self.last_refresh > STATE_REFRESH:
                self.last_refresh = time.time()
                self.publish()
            elif changed:
                self.publish(changed)
            time.sleep(0.5)

    # ---- page
    def page(self) -> str:
        e = html.escape
        with LOCK:
            devs = sorted(self.devices.values(), key=lambda d: (d.up is not False, netscan.ip_key(d.ip)))
            evs = list(self.events)[-60:][::-1]
        down = sum(1 for d in devs if d.up is False)
        rows = "".join(
            f"<tr><td><span class='dot {'up' if d.up else 'down' if d.up is False else 'unk'}'></span></td>"
            f"<td>{e(d.name)}</td><td>{e(d.ip)}</td><td class=mut>{e(d.mac)}</td><td>{e(d.role)}</td>"
            f"<td>{e('down since ' + local_time(d.down_since) if d.up is False else local_time(d.last_seen))}</td>"
            f"<td>{len(d.blips) or ''}</td><td class=mut>{e(d.method)}{' · fast' if d.fast else ''}</td></tr>"
            for d in devs)
        evrows = "".join(f"<tr><td class=mut>{e(x.get('when', ''))}</td><td>{e(x.get('kind', ''))}</td>"
                         f"<td>{e(x.get('text', ''))}</td></tr>" for x in evs)
        status = "scanning now…" if self.scanning else f"last scan {local_time(self.last_scan) or 'never'}"
        return f"""<!doctype html><html><head><meta charset="utf-8"><meta http-equiv="refresh" content="30">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>NetMap</title><style>
:root{{--bg:#fff;--fg:#1d1d1f;--mut:#6e6e73;--line:#e5e5ea;--hd:#f5f5f7;--up:#1f9d55;--down:#d93025}}
@media (prefers-color-scheme:dark){{:root{{--bg:#111;--fg:#eee;--mut:#999;--line:#333;--hd:#1c1c1e}}}}
body{{background:var(--bg);color:var(--fg);font:14px/1.4 -apple-system,Segoe UI,Roboto,sans-serif;margin:16px}}
table{{border-collapse:collapse;width:100%;margin:8px 0 24px}} th,td{{border-bottom:1px solid var(--line);padding:4px 8px;text-align:left}}
th{{background:var(--hd)}} .mut{{color:var(--mut)}} .dot{{display:inline-block;width:10px;height:10px;border-radius:50%;background:var(--mut)}}
.dot.up{{background:var(--up)}} .dot.down{{background:var(--down)}} .wrap{{overflow-x:auto}}
a,button{{font:inherit}} button{{padding:4px 12px}} .bar{{display:flex;gap:12px;align-items:center;flex-wrap:wrap}}</style></head><body>
<h2>NetMap · {e(self.o['site'])}</h2>
<div class=bar><span><b>{len(devs) - down}</b> of {len(devs)} up{f', <b style="color:var(--down)">{down} down</b>' if down else ''}</span>
<span class=mut>{e(status)}</span>
<form method=post action="scan"><button {'disabled' if self.scanning else ''}>Scan now</button></form>
<a href="report" target=_blank>Network map</a><a href="devices.csv">devices.csv</a><a href="networks.csv">networks.csv</a></div>
<p class=mut>Watching every row with reserve=Y in devices.csv (edit it in the Samba share <code>share/netmap/plan</code>).
"Blips" = outages shorter than the notify delay in the last 24 h - a device rebooting on its own.</p>
<div class=wrap><table><tr><th></th><th>name</th><th>ip</th><th>mac</th><th>role</th><th>last seen</th><th>blips 24h</th><th>check</th></tr>{rows}</table></div>
<h3>Events</h3><div class=wrap><table>{evrows or '<tr><td class=mut>none yet</td></tr>'}</table></div></body></html>"""


def make_handler(svc: Service):
    files = {"report": ("report.html", "text/html; charset=utf-8"), "devices.csv": ("devices.csv", "text/csv"),
             "networks.csv": ("networks.csv", "text/csv"), "scan.json": ("scan.json", "application/json")}

    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def _send(self, code: int, body: bytes, ctype: str = "text/html; charset=utf-8", extra=None):
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            for k, v in (extra or {}).items():
                self.send_header(k, v)
            self.end_headers()
            self.wfile.write(body)

        def _allowed(self) -> bool:
            if self.client_address[0] in INGRESS_PEERS:
                return True
            self._send(403, b"open NetMap from the Home Assistant sidebar")
            return False

        def do_GET(self):
            if not self._allowed():
                return
            name = self.path.split("?", 1)[0].rsplit("/", 1)[-1]
            if name in files:
                fn, ctype = files[name]
                try:
                    with open(os.path.join(PLAN, fn), "rb") as f:
                        body = f.read()
                except OSError:
                    return self._send(404, b"no scan yet")
                extra = {} if fn.endswith(".html") else {"Content-Disposition": f'attachment; filename="{fn}"'}
                return self._send(200, body, ctype, extra)
            if name == "events.json":
                return self._send(200, json.dumps(list(svc.events)).encode(), "application/json")
            self._send(200, svc.page().encode())

        def do_POST(self):
            if not self._allowed():
                return
            if self.path.rstrip("/").endswith("scan"):
                svc.scan_now.set()
            prefix = self.headers.get("X-Ingress-Path", "")
            self._send(303, b"", extra={"Location": prefix + "/" if prefix else "./"})

    return H


def main() -> int:
    opts = load_options()
    os.makedirs(DATA, exist_ok=True)
    os.makedirs(PLAN, exist_ok=True)
    os.makedirs(KNOWN, exist_ok=True)
    token = os.environ.get("SUPERVISOR_TOKEN") or os.environ.get("HASSIO_TOKEN")
    if not token:
        log("no SUPERVISOR_TOKEN: Home Assistant sensors and notifications are off")
    svc = Service(opts, HA(token))
    svc.load_watch()
    threading.Thread(target=svc.scan_loop, daemon=True, name="scan").start()
    threading.Thread(target=svc.monitor_loop, daemon=True, name="monitor").start()
    srv = ThreadingHTTPServer(("0.0.0.0", INGRESS_PORT), make_handler(svc))
    log(f"NetMap {netscan.VERSION} for {opts['site']}: page on :{INGRESS_PORT} (ingress), plan in {PLAN}")
    srv.serve_forever()
    return 0


if __name__ == "__main__":
    sys.exit(main())
