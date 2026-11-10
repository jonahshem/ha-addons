#!/usr/bin/env python3
"""
netscan.py - map a client network before we take it over, and write a plan that
unifi_import.py can load into a UniFi console.

Run it on a laptop plugged into the client's LAN. Python 3.8+, standard library only, no
admin rights needed, works on Windows, macOS and Linux.

    python3 netscan.py --site "Sam Stern" --known "Sam Stern IP.txt"
    python3 netscan.py --subnet 192.168.1.0/24 --subnet 10.101.38.0/24 --out sam-stern

What it finds, per address:
  alive        ping, a TCP connect (open OR refused both prove a host), an ARP reply, or an
               answer to NetBIOS / mDNS / SSDP
  MAC          from this machine's ARP table after the sweep. Only for hosts on the same
               segment as the laptop: a routed subnet has no MACs (NetBIOS can still give one)
  vendor       IEEE OUI list (downloaded once into ~/.cache/netmap, or --oui FILE), else a
               small built-in table. Randomised (private) MACs are flagged: phones, never reserve
  names        reverse DNS, NetBIOS name, mDNS hostname + service names, UPnP friendlyName,
               HTTP <title>, Crestron console `ver`, and --known lists (the old integrator's
               IP sheet)
  services     TCP connect on PORTS below; banners for SSH/FTP/telnet/HTTP/Crestron
Probes only read: the one thing sent to a device's console is `ver` on Crestron 41795.

Output (in --out, default ./netmap-<site>):
  scan.json      everything found, raw. Re-running with the same --out merges into it, so a
                 second pass after people turn things on adds to the first.
  devices.csv    one row per device + the plan columns unifi_import.py reads:
                   reserve (Y/N), network, fixed_ip, unifi_name
                 Edit those in Excel. A re-scan keeps your edits (matched by MAC, else IP).
  networks.csv   one row per subnet: name, vlan, gateway_cidr, dhcp_start, dhcp_stop, dns.
                 Suggested once, then left alone so your edits survive re-scans.
  report.html    the map: per-subnet tables, roles, warnings. Open it in a browser.
"""
from __future__ import annotations

import argparse
import concurrent.futures as cf
import csv
import datetime as dt
import html
import http.client
import ipaddress
import json
import os
import platform
import random
import re
import shutil
import socket
import ssl
import struct
import subprocess
import sys
import threading
import time
import urllib.request
import xml.etree.ElementTree as ET

VERSION = "1.0.0"
IS_WIN = os.name == "nt"
IS_MAC = sys.platform == "darwin"

# First pass: cheap ports that most devices answer on, open or refused.
QUICK_PORTS = [80, 443, 22, 23, 41794, 554, 445, 8080]
# Second pass on every live host. Deliberately NOT probed: Modbus 502 and CoolMasterNet 10102.
# Those boxes take very few TCP sessions and a processor polls them; at Sam Stern the HVAC AP3
# power-cycles every T3 board when its Modbus link drops. They are recognised by vendor/name.
PORTS = {
    21: "ftp", 22: "ssh", 23: "telnet", 53: "dns", 80: "http", 81: "http-alt", 139: "netbios",
    443: "https", 445: "smb", 554: "rtsp", 631: "ipp", 1400: "sonos",
    1883: "mqtt", 3389: "rdp", 5000: "http-5000", 5001: "https-5001", 5004: "autonomic",
    5020: "control4", 5900: "vnc", 7000: "airplay", 7001: "dw-nvr/airplay", 8000: "http-8000",
    8008: "googlecast", 8009: "googlecast-tls", 8080: "http-8080", 8123: "home-assistant",
    8443: "https-8443", 9100: "printer", 9621: "russound",
    41794: "crestron-cip", 41795: "crestron-console", 41796: "crestron-cip-tls",
    41797: "crestron-console-tls", 49152: "upnp",
}
HTTP_PORTS = {80: False, 81: False, 443: True, 5000: False, 5001: True, 7001: False,
              8000: False, 8080: False, 8123: False, 8443: True}
MDNS_SERVICES = ["_services._dns-sd._udp.local", "_airplay._tcp.local", "_raop._tcp.local",
                 "_googlecast._tcp.local", "_sonos._tcp.local", "_hap._tcp.local",
                 "_ipp._tcp.local", "_printer._tcp.local", "_http._tcp.local",
                 "_smb._tcp.local", "_companion-link._tcp.local", "_spotify-connect._tcp.local",
                 "_home-assistant._tcp.local", "_workstation._tcp.local", "_device-info._tcp.local"]

# Used only when the IEEE list can't be had (no internet on site and no --oui file).
# Six hex digits -> vendor. Kept to prefixes seen on our own jobs or well known.
OUI_FALLBACK = {
    "00107F": "Crestron", "C44268": "Crestron", "000EC6": "ASIX (Temco/Hii boards)",
    "0003C5": "Mobotix", "0002D1": "Vivotek", "2857BE": "Hikvision", "BCAD28": "Hikvision",
    "4CBD8F": "Hikvision", "C056E3": "Hikvision",
    "000E58": "Sonos", "5CAAFD": "Sonos", "B8E937": "Sonos", "347E5C": "Sonos",
    "542A1B": "Sonos", "949F3E": "Sonos", "F0F6C1": "Sonos", "48A6B8": "Sonos",
    "245A4C": "Ubiquiti", "F09FC2": "Ubiquiti", "788A20": "Ubiquiti", "FCECDA": "Ubiquiti",
    "B4FBE4": "Ubiquiti", "74ACB9": "Ubiquiti", "E063DA": "Ubiquiti", "D8B370": "Ubiquiti",
    "687251": "Ubiquiti", "802AA8": "Ubiquiti", "0418D6": "Ubiquiti", "E43883": "Ubiquiti",
    "1C6A1B": "Ubiquiti", "60224D": "Ubiquiti", "AC8BA9": "Ubiquiti",
    "000C29": "VMware", "BC2411": "Proxmox VM", "B827EB": "Raspberry Pi",
    "DCA632": "Raspberry Pi", "E45F01": "Raspberry Pi", "2CCF67": "Raspberry Pi",
    "001788": "Philips Hue", "ECB5FA": "Philips Hue",
}

LOCK = threading.Lock()


def log(msg: str) -> None:
    with LOCK:
        print(msg, flush=True)


# --------------------------------------------------------------------------- small helpers

def norm_mac(s: str) -> str:
    """'0:e:c6:f1:c9:36' / '00-0E-C6-F1-C9-36' / '000ec6f1c936' -> '00:0e:c6:f1:c9:36'."""
    s = (s or "").strip().lower()
    if re.fullmatch(r"[0-9a-f]{1,2}([:-][0-9a-f]{1,2}){5}", s):
        return ":".join(p.zfill(2) for p in re.split(r"[:-]", s))
    h = re.sub(r"[^0-9a-f]", "", s)
    return ":".join(h[i:i + 2] for i in range(0, 12, 2)) if len(h) == 12 else ""


def is_random_mac(mac: str) -> bool:
    """Locally administered bit set: phones' private Wi-Fi addresses, VMs, some bridges."""
    try:
        return bool(int(mac.split(":")[0], 16) & 0x02)
    except (ValueError, IndexError):
        return False


def usable_mac(mac: str) -> bool:
    if not mac or mac in ("00:00:00:00:00:00", "ff:ff:ff:ff:ff:ff"):
        return False
    return not (int(mac.split(":")[0], 16) & 0x01)  # multicast


def ip_key(ip: str):
    try:
        return (0, int(ipaddress.ip_address(ip)))
    except ValueError:
        return (1, ip)


def run(cmd: list, timeout: float = 10) -> str:
    kw = {}
    if IS_WIN:
        kw["creationflags"] = 0x08000000  # CREATE_NO_WINDOW
    try:
        p = subprocess.run(cmd, capture_output=True, timeout=timeout, **kw)
        return p.stdout.decode("utf-8", "replace") + p.stderr.decode("utf-8", "replace")
    except (OSError, subprocess.SubprocessError):
        return ""


# --------------------------------------------------------------------------- local interface

def primary_ip() -> str | None:
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 53))  # no packet is sent; picks the default-route interface
        return s.getsockname()[0]
    except OSError:
        return None
    finally:
        s.close()


def local_network() -> tuple[str | None, str | None, str | None]:
    """(own ip, network cidr, default gateway) for the interface with the default route."""
    ip = primary_ip()
    if not ip:
        return None, None, None
    prefix, gw = None, None
    if IS_WIN:
        out = run(["ipconfig"])
        blocks = re.split(r"\r?\n(?=\S)", out)
        for b in blocks:
            if re.search(r":\s*" + re.escape(ip) + r"\b", b):
                m = re.search(r"(?:Subnet Mask|Masque|Subnetzmaske)[ .]*:\s*([\d.]+)", b)
                if m:
                    prefix = ipaddress.ip_network(f"0.0.0.0/{m.group(1)}").prefixlen
                after = re.split(r"gateway", b, maxsplit=1, flags=re.I)
                gws = re.findall(r"\b(\d{1,3}(?:\.\d{1,3}){3})\b", after[1]) if len(after) > 1 else []
                gw = gws[0] if gws else None
    elif os.path.exists("/proc/net/route"):
        with open("/proc/net/route") as f:
            rows = [l.split() for l in f.read().splitlines()[1:] if l.strip()]
        def h2ip(h):
            return socket.inet_ntoa(struct.pack("<L", int(h, 16)))
        for r in rows:
            dest, gwh, mask = h2ip(r[1]), r[2], h2ip(r[7])
            if dest == "0.0.0.0" and gwh != "00000000" and gw is None:
                gw = h2ip(gwh)
            elif dest != "0.0.0.0":
                net = ipaddress.ip_network(f"{dest}/{mask}", strict=False)
                if ipaddress.ip_address(ip) in net and (prefix is None or net.prefixlen > prefix):
                    prefix = net.prefixlen
    else:  # macOS / BSD
        m = re.search(r"inet " + re.escape(ip) + r" netmask (0x[0-9a-f]+)", run(["ifconfig"]))
        if m:
            prefix = bin(int(m.group(1), 16)).count("1")
        m = re.search(r"gateway:\s*([\d.]+)", run(["route", "-n", "get", "default"]))
        gw = m.group(1) if m else None
    if prefix is None:
        prefix = 24
        log(f"  (could not read the netmask; assuming /{prefix})")
    return ip, str(ipaddress.ip_network(f"{ip}/{prefix}", strict=False)), gw


# --------------------------------------------------------------------------- ARP table

def arp_table() -> dict[str, str]:
    """ip -> mac for every resolved neighbour this machine knows."""
    out: dict[str, str] = {}
    if not IS_WIN and os.path.exists("/proc/net/arp"):
        with open("/proc/net/arp") as f:
            for line in f.read().splitlines()[1:]:
                p = line.split()
                if len(p) >= 4 and p[2] != "0x0":
                    mac = norm_mac(p[3])
                    if usable_mac(mac):
                        out[p[0]] = mac
        return out
    text = run(["arp", "-a"] if IS_WIN else ["arp", "-an"])
    for m in re.finditer(r"\(?(\d{1,3}(?:\.\d{1,3}){3})\)?\s+(?:at\s+)?"
                         r"([0-9a-fA-F]{1,2}(?:[:-][0-9a-fA-F]{1,2}){5})", text):
        mac = norm_mac(m.group(2))
        if usable_mac(mac):
            out[m.group(1)] = mac
    return out


# --------------------------------------------------------------------------- OUI vendors

class Oui:
    URL = "https://standards-oui.ieee.org/oui/oui.csv"

    def __init__(self, path: str | None, offline: bool):
        self.table: dict[str, str] = {}
        self.source = "built-in table"
        cache = path or os.path.join(os.path.expanduser("~"), ".cache", "netmap", "oui.csv")
        if not offline and not os.path.exists(cache):
            try:
                os.makedirs(os.path.dirname(cache), exist_ok=True)
                req = urllib.request.Request(self.URL, headers={"User-Agent": "Mozilla/5.0 netmap"})
                with urllib.request.urlopen(req, timeout=20) as r, open(cache + ".part", "wb") as f:
                    shutil.copyfileobj(r, f)
                os.replace(cache + ".part", cache)
            except Exception as e:  # noqa: BLE001 - offline on site is normal
                log(f"  (IEEE vendor list not downloaded: {e.__class__.__name__}; using built-in table)")
        if os.path.exists(cache):
            try:
                with open(cache, newline="", encoding="utf-8", errors="replace") as f:
                    for row in csv.reader(f):
                        if len(row) >= 3 and re.fullmatch(r"[0-9A-F]{6}", row[1]):
                            self.table[row[1]] = row[2].strip()
                self.source = f"IEEE list ({len(self.table)} vendors)"
            except OSError:
                pass

    def vendor(self, mac: str) -> str:
        if not mac:
            return ""
        if is_random_mac(mac):
            return "(private/random MAC)"
        key = mac.replace(":", "")[:6].upper()
        v = self.table.get(key) or OUI_FALLBACK.get(key, "")
        return short_vendor(v)


def short_vendor(v: str) -> str:
    v = re.sub(r",?\s*(Inc\.?|LLC|Ltd\.?|Co\.,? ?Ltd\.?|Corporation|Corp\.?|GmbH|S\.A\.|AG|"
               r"Limited|Technologies|Technology|Electronics)\b\.?", "", v, flags=re.I)
    return re.sub(r"\s{2,}", " ", v).strip(" ,.")


# --------------------------------------------------------------------------- DNS wire format

def dns_name(name: str) -> bytes:
    return b"".join(bytes([len(p)]) + p.encode() for p in name.rstrip(".").split(".")) + b"\0"


def dns_query(names: list[tuple[str, int]], tid: int = 0) -> bytes:
    body = b"".join(dns_name(n) + struct.pack(">HH", t, 1) for n, t in names)
    return struct.pack(">HHHHHH", tid, 0, len(names), 0, 0, 0) + body


def read_name(buf: bytes, off: int) -> tuple[str, int]:
    labels, jumped, end, hops = [], False, off, 0
    while off < len(buf) and hops < 64:
        n = buf[off]
        if n == 0:
            off += 1
            break
        if n & 0xC0 == 0xC0:
            if off + 1 >= len(buf):
                break
            if not jumped:
                end = off + 2
            off = ((n & 0x3F) << 8) | buf[off + 1]
            jumped, hops = True, hops + 1
            continue
        labels.append(buf[off + 1:off + 1 + n].decode("utf-8", "replace"))
        off += 1 + n
    return ".".join(labels), (end if jumped else off)


def parse_dns(buf: bytes) -> list[tuple[str, int, object]]:
    """All answer/authority/additional records as (name, type, value)."""
    if len(buf) < 12:
        return []
    _, _, qd, an, ns, ar = struct.unpack(">HHHHHH", buf[:12])
    off, out = 12, []
    try:
        for _ in range(qd):
            _, off = read_name(buf, off)
            off += 4
        for _ in range(an + ns + ar):
            name, off = read_name(buf, off)
            rtype, _, _, rdlen = struct.unpack(">HHIH", buf[off:off + 10])
            off += 10
            rd = buf[off:off + rdlen]
            if rtype == 1 and rdlen == 4:
                val = socket.inet_ntoa(rd)
            elif rtype == 12:
                val = read_name(buf, off)[0]
            elif rtype == 33:
                val = (struct.unpack(">H", rd[4:6])[0], read_name(buf, off + 6)[0])
            elif rtype == 16:
                val, i = [], 0
                while i < len(rd):
                    val.append(rd[i + 1:i + 1 + rd[i]].decode("utf-8", "replace"))
                    i += 1 + rd[i]
            else:
                val = None
            out.append((name, rtype, val))
            off += rdlen
    except (struct.error, IndexError):
        pass
    return out


# --------------------------------------------------------------------------- probes

def ping(ip: str, timeout_ms: int = 700) -> bool:
    exe = shutil.which("ping")
    if not exe:
        return False
    if IS_WIN:
        cmd = [exe, "-n", "1", "-w", str(timeout_ms), ip]
    elif IS_MAC:
        cmd = [exe, "-c", "1", "-t", "1", ip]
    else:
        cmd = [exe, "-c", "1", "-W", "1", ip]
    out = run(cmd, timeout=3)
    return "ttl=" in out.lower()  # Windows exits 0 on "destination unreachable" too


def tcp_state(ip: str, port: int, timeout: float) -> str:
    """'open', 'refused' (host exists, port closed) or '' (no answer)."""
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.settimeout(timeout)
    try:
        s.connect((ip, port))
        return "open"
    except ConnectionRefusedError:
        return "refused"
    except OSError:
        return ""
    finally:
        s.close()


def udp_poke(ip: str) -> None:
    """One UDP datagram to the discard port: the OS ARPs for the address first, so a host that
    ignores ping and every TCP port still lands in the ARP table."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.sendto(b"\0", (ip, 9))
    except OSError:
        pass
    finally:
        s.close()


LOGIN_PROMPT = re.compile(rb"login|user\s*name|password", re.I)


def read_banner(ip: str, port: int, send: bytes | None = None, wait: float = 1.5) -> str:
    """Whatever the port says first; with `send`, also the reply to it - unless the first thing
    it says is a login prompt, in which case nothing is sent (a 'ver' typed into a Username
    prompt would count as a failed login)."""
    try:
        with socket.create_connection((ip, port), timeout=2.5) as s:
            s.settimeout(wait)
            data = b""
            if send is not None:
                try:
                    data += s.recv(4096)  # a prompt, if it sends one first
                except OSError:
                    pass
                if LOGIN_PROMPT.search(data):
                    return "(asks for a login) " + re.sub(r"[^\x20-\x7e]", " ",
                                                          data.decode("latin-1")).strip()[:80]
                s.sendall(send)
            end = time.time() + wait + 1
            while time.time() < end and len(data) < 8192:
                try:
                    chunk = s.recv(4096)
                except OSError:
                    break
                if not chunk:
                    break
                data += chunk
            text = data.decode("latin-1", "replace")
            text = re.sub(r"\xff[\xfb-\xfe].", "", text)  # telnet negotiation
            return re.sub(r"[^\x20-\x7e\n]", "", text).strip()
    except OSError:
        return ""


def http_probe(ip: str, port: int, tls: bool) -> dict:
    ctx = ssl._create_unverified_context() if tls else None
    try:
        c = (http.client.HTTPSConnection(ip, port, timeout=3, context=ctx) if tls
             else http.client.HTTPConnection(ip, port, timeout=3))
        c.request("GET", "/", headers={"User-Agent": "netmap", "Host": ip})
        r = c.getresponse()
        body = r.read(65536).decode("utf-8", "replace")
        c.close()
    except Exception:  # noqa: BLE001 - anything odd means "no web page here"
        return {}
    out = {"status": r.status}
    if r.getheader("Server"):
        out["server"] = r.getheader("Server")[:80]
    realm = re.search(r'realm="([^"]+)"', r.getheader("WWW-Authenticate") or "")
    if realm:
        out["realm"] = realm.group(1)[:80]
    if r.getheader("Location"):
        out["location"] = r.getheader("Location")[:120]
    t = re.search(r"<title[^>]*>(.*?)</title>", body, re.I | re.S)
    if t:
        out["title"] = re.sub(r"\s+", " ", html.unescape(t.group(1))).strip()[:100]
    return out


def reverse_dns(ip: str) -> str:
    try:
        return socket.gethostbyaddr(ip)[0]
    except (OSError, UnicodeError):
        return ""


NB_QUERY = (b"\x13\x37\x00\x00\x00\x01\x00\x00\x00\x00\x00\x00\x20" + b"CK" + b"A" * 30 +
            b"\x00\x00\x21\x00\x01")


def parse_netbios(data: bytes) -> dict:
    if len(data) < 57:
        return {}
    n = data[56]
    out, off = {}, 57
    for _ in range(n):
        if off + 18 > len(data):
            break
        name = data[off:off + 15].decode("latin-1").strip()
        suffix, flags = data[off + 15], struct.unpack(">H", data[off + 16:off + 18])[0]
        group = bool(flags & 0x8000)
        if suffix == 0 and not group and "name" not in out:
            out["name"] = name
        elif suffix == 0 and group and "workgroup" not in out:
            out["workgroup"] = name
        off += 18
    if off + 6 <= len(data):
        mac = norm_mac(data[off:off + 6].hex())
        if usable_mac(mac):
            out["mac"] = mac
    return out


def netbios(ip: str) -> dict:
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.settimeout(1.0)
    try:
        s.sendto(NB_QUERY, (ip, 137))
        return parse_netbios(s.recvfrom(2048)[0])
    except OSError:
        return {}
    finally:
        s.close()


def mdns_reverse(ip: str) -> str:
    """Unicast PTR query to the host's own mDNS responder (legacy unicast, RFC 6762 6.7)."""
    rev = ".".join(reversed(ip.split("."))) + ".in-addr.arpa"
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.settimeout(1.0)
    try:
        s.sendto(dns_query([(rev, 12)], tid=random.randint(1, 65535)), (ip, 5353))
        for name, rtype, val in parse_dns(s.recvfrom(4096)[0]):
            if rtype == 12 and isinstance(val, str) and val:
                return val
    except OSError:
        pass
    finally:
        s.close()
    return ""


def crestron_ver(text: str) -> dict:
    """'TSW-752 [v3.002.1061 (...), #00A0B1C2] @E-00107f123456' -> model/firmware/mac."""
    out = {}
    m = re.search(r"^\s*([A-Z][A-Z0-9-]{2,}(?:\s+\w+){0,3}?)\s+\[v?([\d.]+)", text, re.M)
    if m:
        out["model"], out["firmware"] = m.group(1).strip(), m.group(2)
    m = re.search(r"@E-([0-9a-fA-F]{12})", text)
    if m:
        out["mac"] = norm_mac(m.group(1))
    m = re.search(r"#([0-9A-F]{8})", text)
    if m:
        out["tsid"] = m.group(1)
    return out


# --------------------------------------------------------------------------- multicast discovery

def mdns_browse(local_ip: str, seconds: float = 4.0) -> dict[str, dict]:
    """Ask the LAN for common service types; replies come back unicast to our port."""
    found: dict[str, dict] = {}
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 1)
        s.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_IF, socket.inet_aton(local_ip))
        s.bind((local_ip, 0))
        q = dns_query([(n, 12) for n in MDNS_SERVICES])
        s.sendto(q, ("224.0.0.251", 5353))
        end = time.time() + seconds
        asked_types: set[str] = set()
        while time.time() < end:
            s.settimeout(max(0.05, end - time.time()))
            try:
                data, (src, _) = s.recvfrom(9000)
            except OSError:
                break
            d = found.setdefault(src, {"services": set(), "names": set(), "txt": {}, "host": ""})
            more = []
            for name, rtype, val in parse_dns(data):
                if rtype == 12 and name == "_services._dns-sd._udp.local" and isinstance(val, str):
                    if val not in asked_types and val not in MDNS_SERVICES:
                        asked_types.add(val)
                        more.append((val, 12))
                elif rtype == 12 and isinstance(val, str):
                    d["services"].add(name.replace(".local", ""))
                    inst = val.split("._", 1)[0]
                    if inst:
                        d["names"].add(inst)
                elif rtype == 33 and isinstance(val, tuple):
                    if val[1]:
                        d["host"] = val[1]
                elif rtype == 16 and isinstance(val, list):
                    for kv in val:
                        k, _, v = kv.partition("=")
                        if k in ("md", "fn", "model", "am", "ty", "usb_MDL", "mdl", "manufacturer"):
                            d["txt"][k] = v[:80]
            if more:
                s.sendto(dns_query(more[:20]), ("224.0.0.251", 5353))
    except OSError as e:
        log(f"  (mDNS browse failed: {e})")
    finally:
        s.close()
    for d in found.values():
        d["services"], d["names"] = sorted(d["services"]), sorted(d["names"])
    return found


def ssdp_discover(local_ip: str, seconds: float = 4.0) -> dict[str, dict]:
    msg = ("M-SEARCH * HTTP/1.1\r\nHOST: 239.255.255.250:1900\r\nMAN: \"ssdp:discover\"\r\n"
           "MX: 2\r\nST: ssdp:all\r\n\r\n").encode()
    found: dict[str, dict] = {}
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 2)
        s.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_IF, socket.inet_aton(local_ip))
        s.bind((local_ip, 0))
        for _ in range(2):
            s.sendto(msg, ("239.255.255.250", 1900))
        end = time.time() + seconds
        while time.time() < end:
            s.settimeout(max(0.05, end - time.time()))
            try:
                data, (src, _) = s.recvfrom(4096)
            except OSError:
                break
            hdr = {}
            for line in data.decode("latin-1").split("\r\n")[1:]:
                k, _, v = line.partition(":")
                hdr[k.strip().lower()] = v.strip()
            d = found.setdefault(src, {"locations": [], "server": ""})
            loc = hdr.get("location", "")
            if loc and loc not in d["locations"] and len(d["locations"]) < 3:
                d["locations"].append(loc)
            d["server"] = d["server"] or hdr.get("server", "")[:100]
    except OSError as e:
        log(f"  (SSDP discovery failed: {e})")
    finally:
        s.close()
    return found


def upnp_description(url: str) -> dict:
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "netmap"})
        with urllib.request.urlopen(req, timeout=3, context=ssl._create_unverified_context()) as r:
            root = ET.fromstring(r.read(262144))
    except Exception:  # noqa: BLE001
        return {}
    out = {}
    for el in root.iter():
        tag = el.tag.rsplit("}", 1)[-1]
        if tag in ("friendlyName", "manufacturer", "modelName", "modelNumber", "serialNumber",
                   "roomName") and (el.text or "").strip() and tag not in out:
            out[tag] = el.text.strip()[:100]
    return out


# --------------------------------------------------------------------------- known lists

IP_RE = re.compile(r"\b(\d{1,3}(?:\.\d{1,3}){3})\b")
MAC_RE = re.compile(r"\b([0-9A-Fa-f]{2}(?:[:-][0-9A-Fa-f]{2}){5})\b")


def parse_known(path: str) -> list[dict]:
    """A free-form IP sheet (or a CSV with name/ip/mac columns): every line holding an IPv4
    address becomes {name, ip, mac, note}. The name is the text before the address."""
    with open(path, encoding="utf-8", errors="replace") as f:
        text = f.read()
    rows = []
    first = text.splitlines()[0].lower() if text.strip() else ""
    if "," in first and "ip" in first:
        for r in csv.DictReader(text.splitlines()):
            r = {k.strip().lower(): (v or "").strip() for k, v in r.items() if k}
            ip = r.get("ip") or r.get("ip address") or r.get("address") or ""
            if IP_RE.fullmatch(ip):
                rows.append({"name": r.get("name") or r.get("device") or "", "ip": ip,
                             "mac": norm_mac(r.get("mac") or ""), "note": r.get("note") or "",
                             "source": os.path.basename(path)})
        return rows
    heading, used = "", False
    for line in text.splitlines():
        # an address inside a URL (http://user:pw@1.2.3.4/...) is a reference, not a device
        m = next((x for x in IP_RE.finditer(line) if line[max(0, x.start() - 1):x.start()] not in ("/", "@")),
                 None)
        if not m:
            if IP_RE.search(line):
                continue
            if line.strip().endswith(":"):
                heading, used = line.strip().rstrip(":").strip(), False
            elif line.startswith(("=", "*")) or (not line.strip() and used):
                heading = ""
            continue
        used = True
        try:
            ip = str(ipaddress.ip_address(m.group(1)))
        except ValueError:
            continue
        if not ipaddress.ip_address(ip).is_private:
            continue
        name = re.sub(r"[\s.:=-]+$", "", line[:m.start()]).strip()
        name = re.sub(r"^(IP|Addr|IPAddr|IP Address)$", "", name, flags=re.I).strip()
        mm = MAC_RE.search(line)
        rest = line[m.end():]
        rest = re.sub(r"^:\d+", "", rest)
        if mm:
            rest = rest.replace(mm.group(1), "")
        rest = re.sub(r"\s+", " ", rest).strip()
        # never carry credentials into the plan files
        rest = re.sub(r"\b(pass(word)?|pw|pin|wpa)\b\s*[:=]?\s*\S+", "[secret removed]", rest, flags=re.I)
        rest = re.sub(r"\b(admin|root|user|crestron|chdevice|administrator|operator)\s*:\s*\S+",
                      "[login removed]", rest, flags=re.I)
        if re.search(r"\b(pass(word)?|pw|pin|wpa|login|user(name)?|ssid)\b", name, re.I):
            name = ""  # "Pass  hunter2 192.168.1.9": the text before the address is a secret
        if not name:  # "10.101.38.107  rear deck cam" / "IP 192.168.1.188" under "Digital Watchdog NVR:"
            name, rest = (rest, heading) if rest and not rest.startswith("[") else (heading, rest)
        elif heading and heading.lower() not in name.lower():
            rest = (heading + "; " + rest).strip("; ")
        rows.append({"name": name[:60], "ip": ip, "mac": norm_mac(mm.group(1)) if mm else "",
                     "note": rest[:120], "source": os.path.basename(path)})
    return rows


# --------------------------------------------------------------------------- classification

def classify(d: dict) -> str:
    v = (d.get("vendor") or "").lower()
    ports = set(d.get("ports", {}))
    txt = " ".join(str(x) for x in [d.get("crestron", {}).get("model", ""), d.get("upnp", {}),
                                    d.get("mdns", {}).get("txt", {}), d.get("http", {}),
                                    d.get("known_name", ""), d.get("hostname", "")]).lower()
    svc = " ".join(d.get("mdns", {}).get("services", [])).lower()
    if d.get("self"):
        return "this computer"
    if d.get("gateway"):
        return "router (current gateway)"
    if "crestron" in v or ports & {41794, 41795, 41796, 41797}:
        model = d.get("crestron", {}).get("model", "")
        if re.search(r"\b(TSW|TS-|TSR|TST)", model):
            return "Crestron touch panel"
        if re.search(r"\b(CP\d|MC\d|PRO\d|AV\d|DIN-AP|RMC|CP4)", model):
            return "Crestron processor"
        return "Crestron"
    if "ubiquiti" in v or "unifi" in txt:
        return "UniFi / Ubiquiti"
    if any(x in v for x in ("araknis", "cisco", "netgear", "tp-link", "ruckus", "aruba")) \
            or re.search(r"\b(switch|poe|access point)\b", txt):
        return "network switch / AP"
    if any(x in v for x in ("mobotix", "hikvision", "vivotek", "amcrest", "axis", "dahua",
                            "digital watchdog")) \
            or re.search(r"\b(cam|camera|nvr|dvr)\b", txt) or (554 in ports and 9100 not in ports):
        return "camera / NVR"
    if "sonos" in v or 1400 in ports or "_sonos" in svc:
        return "Sonos"
    if "_googlecast" in svc or 8008 in ports:
        return "Google Cast / TV"
    if "_airplay" in svc or "appletv" in txt or "homepod" in txt:
        return "Apple TV / AirPlay"
    if 9100 in ports or 631 in ports or "_ipp" in svc or "_printer" in svc:
        return "printer"
    if "asix" in v or 502 in ports or "tstat" in txt or "closet" in txt:
        return "controller (Modbus / HVAC board)"
    if 10102 in ports or "coolmaster" in txt:
        return "CoolMaster"
    if 8123 in ports or "home assistant" in txt:
        return "Home Assistant"
    if d.get("random_mac"):
        return "phone / tablet (private MAC)"
    if ports & {3389, 445, 139} or "_workstation" in svc or "_smb" in svc:
        return "computer / NAS"
    if "apple" in v:
        return "Apple device"
    if ports:
        return "device with services"
    return "unknown"


NO_RESERVE_ROLES = {"this computer", "router (current gateway)", "phone / tablet (private MAC)",
                    "computer / NAS", "Apple device", "unknown"}


def default_reserve(d: dict) -> str:
    """Y for anything we will want at a fixed address: control gear, AV, cameras, and every
    address the old integrator wrote down. Never without a real (not private) MAC."""
    if not d.get("mac") or d.get("random_mac") or d.get("self") or d.get("gateway"):
        return "N"
    if d.get("known_name"):
        return "Y"
    return "N" if d["role"] in NO_RESERVE_ROLES else "Y"


def best_name(d: dict) -> str:
    cands = [d.get("known_name"), d.get("upnp", {}).get("friendlyName"),
             (d.get("mdns", {}).get("txt", {}) or {}).get("fn"),
             *(d.get("mdns", {}).get("names") or []), d.get("netbios", {}).get("name"),
             (d.get("mdns_host") or "").replace(".local", ""), d.get("crestron", {}).get("model"),
             (d.get("hostname") or "").split(".")[0], d.get("http", {}).get("title")]
    for c in cands:
        c = re.sub(r"\s*\[[0-9a-f]{8,}\]$", "", str(c or ""), flags=re.I)  # "homeassistant [a490e9...]"
        c = re.sub(r"[^\w .()#+/-]", "", c).strip()
        if re.fullmatch(r"[0-9A-Fa-f-]{12,}", c):  # an id, not a name
            continue
        if c and not IP_RE.fullmatch(c) and c.lower() not in ("index", "login", "home", "web"):
            return c[:40]
    v = d.get("vendor") or ""
    return f"{v.split()[0] if v and not v.startswith('(') else 'device'}-{d['ip'].split('.')[-1]}"


# --------------------------------------------------------------------------- the scan

def sweep(targets: list[str], threads: int, quick_timeout: float,
          arp_seen: dict[str, str]) -> dict[str, set]:
    """Which addresses answer, and how. The ARP table is read every 64 hosts into arp_seen:
    on a big sweep the OS starts expiring early entries before the end."""
    alive: dict[str, set] = {}
    have_ping = bool(shutil.which("ping"))

    def one(ip):
        how = set()
        udp_poke(ip)
        if have_ping and ping(ip):
            how.add("ping")
        for p in QUICK_PORTS:
            st = tcp_state(ip, p, quick_timeout)
            if st:
                how.add(f"tcp/{p}" if st == "open" else "tcp-rst")
                if st == "open":
                    break
        return ip, how

    done = 0
    with cf.ThreadPoolExecutor(threads) as ex:
        for ip, how in ex.map(one, targets):
            done += 1
            if how:
                alive[ip] = how
            if done % 64 == 0 or done == len(targets):
                arp_seen.update(arp_table())
                log(f"  swept {done}/{len(targets)}, {len(alive)} answering so far")
    return alive


def deep(ip: str, port_timeout: float) -> dict:
    d: dict = {"ip": ip, "ports": {}}
    with cf.ThreadPoolExecutor(8) as ex:
        states = dict(zip(PORTS, ex.map(lambda p: tcp_state(ip, p, port_timeout), PORTS)))
    d["ports"] = {p: PORTS[p] for p in PORTS if states[p] == "open"}
    for p, tls in HTTP_PORTS.items():
        if p in d["ports"]:
            h = http_probe(ip, p, tls)
            if h:
                d.setdefault("http", {})
                for k, v in h.items():
                    d["http"].setdefault(k, v)
                d.setdefault("http_ports", {})[p] = h
    for p, label in ((22, "ssh"), (21, "ftp"), (23, "telnet")):
        if p in d["ports"]:
            b = read_banner(ip, p, wait=1.2)
            if b:
                d.setdefault("banners", {})[label] = b.splitlines()[0][:120]
    if 41795 in d["ports"]:
        b = read_banner(ip, 41795, send=b"ver\r\n", wait=2.0)
        if b:
            d["crestron"] = {} if b.startswith("(asks for a login)") else crestron_ver(b)
            d["crestron"]["ver"] = " ".join(b.split())[:200]
    if 8008 in d["ports"]:
        try:
            with urllib.request.urlopen(f"http://{ip}:8008/setup/eureka_info?params=name,device_info",
                                        timeout=3) as r:
                j = json.loads(r.read(65536))
            d["cast"] = {"name": j.get("name"), "model": (j.get("device_info") or {}).get("model_name")}
        except Exception:  # noqa: BLE001
            pass
    nb = netbios(ip)
    if nb:
        d["netbios"] = nb
    rd = reverse_dns(ip)
    if rd and rd != ip:
        d["hostname"] = rd
    mh = mdns_reverse(ip)
    if mh:
        d["mdns_host"] = mh
    return d


def subnet_hosts(cidr: str) -> list[str]:
    net = ipaddress.ip_network(cidr, strict=False)
    return [str(h) for h in (net.hosts() if net.prefixlen < 31 else net)]


def suggest_dhcp(cidr: str, gateway: str | None, reserved: set[str]) -> tuple[str, str, str]:
    """The largest run of addresses with no reservation and not the gateway. When that run is
    small, the whole subnet past the first few addresses instead: the UDM's DHCP server (dnsmasq)
    never leases a reserved address to another device, so reservations inside the pool are
    safe. Returns (start, stop, note)."""
    hosts = subnet_hosts(cidr)
    taken = set(reserved) | ({gateway} if gateway else set())
    best, cur = (0, 0), None
    for i, h in enumerate(hosts):
        if h in taken:
            cur = None
            continue
        cur = i if cur is None else cur
        if i - cur > best[1] - best[0]:
            best = (cur, i)
    a, b = best
    if b - a < 60 and len(hosts) > 100:
        return hosts[5], hosts[-1], (f"largest free run was only {b - a + 1}; pool spans the "
                                     f"reservations instead (safe: reserved addresses are never leased)")
    if b - a > 20:
        a, b = a + 5, b - 5  # leave a few spare either side for hand-set devices
    return hosts[a], hosts[b], ""


# --------------------------------------------------------------------------- output

DEV_COLS = ["ip", "mac", "vendor", "role", "best_name", "known_name", "hostname", "netbios",
            "mdns", "upnp", "crestron", "open_ports", "http", "seen_by", "random_mac", "subnet",
            "last_seen", "notes", "reserve", "network", "fixed_ip", "unifi_name"]
PLAN_COLS = ["reserve", "network", "fixed_ip", "unifi_name", "notes"]
NET_COLS = ["name", "vlan", "gateway_cidr", "dhcp_start", "dhcp_stop", "dns1", "dns2",
            "domain", "scanned_from", "notes"]


def device_row(d: dict, nets: dict[str, str]) -> dict:
    up = d.get("upnp", {})
    md = d.get("mdns", {})
    cr = d.get("crestron", {})
    return {
        "ip": d["ip"], "mac": d.get("mac", ""), "vendor": d.get("vendor", ""), "role": d["role"],
        "best_name": d["best_name"], "known_name": d.get("known_name", ""),
        "hostname": d.get("hostname", ""), "netbios": d.get("netbios", {}).get("name", ""),
        "mdns": "; ".join(filter(None, [d.get("mdns_host", ""), ", ".join(md.get("names", [])[:3]),
                                        ", ".join(md.get("services", [])[:5])])),
        "upnp": " | ".join(filter(None, [up.get("friendlyName"), up.get("manufacturer"),
                                         up.get("modelName")])),
        "crestron": " ".join(filter(None, [cr.get("model"), cr.get("firmware")])),
        "open_ports": " ".join(f"{p}/{l}" for p, l in sorted(d.get("ports", {}).items(),
                                                            key=lambda x: int(x[0]))),
        "http": " | ".join(filter(None, [d.get("http", {}).get("title"), d.get("http", {}).get("server"),
                                         d.get("http", {}).get("realm")])),
        "seen_by": " ".join(sorted(d.get("seen_by", []))),
        "random_mac": "Y" if d.get("random_mac") else "",
        "subnet": d.get("subnet", ""), "last_seen": d.get("last_seen", ""),
        "notes": "; ".join(filter(None, [
            d.get("known_note", ""),
            "private MAC but serves ports: a VM/container? reserve by hand if it is fixed"
            if d.get("random_mac") and set(map(int, d.get("ports", {}))) - {49152, 7000, 5000} else ""])),
        "reserve": default_reserve(d),
        "network": nets.get(d.get("subnet", ""), ""),
        "fixed_ip": d["ip"],
        "unifi_name": d["best_name"],
    }


def read_csv(path: str) -> list[dict]:
    if not os.path.exists(path):
        return []
    with open(path, newline="", encoding="utf-8-sig") as f:
        return list(csv.DictReader(f))


def write_csv(path: str, cols: list[str], rows: list[dict]) -> None:
    tmp = path + ".tmp"
    with open(tmp, "w", newline="", encoding="utf-8-sig") as f:  # BOM: Excel opens it as UTF-8
        w = csv.DictWriter(f, fieldnames=cols, extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)
    os.replace(tmp, path)


def write_report(path: str, site: str, scan: dict, rows: list[dict], nets: list[dict],
                 warnings: list[str]) -> None:
    e = html.escape
    by_sub: dict[str, list[dict]] = {}
    for r in rows:
        by_sub.setdefault(r["subnet"] or "other", []).append(r)
    roles: dict[str, int] = {}
    for r in rows:
        roles[r["role"]] = roles.get(r["role"], 0) + 1
    cols = ["ip", "mac", "vendor", "role", "best_name", "known_name", "crestron", "open_ports",
            "http", "reserve", "fixed_ip", "notes"]
    parts = [f"""<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{e(site)} network map</title><style>
:root{{--bg:#fff;--fg:#1d1d1f;--mut:#6e6e73;--line:#e5e5ea;--hd:#f5f5f7;--warn:#b25000}}
@media (prefers-color-scheme:dark){{:root{{--bg:#111;--fg:#eee;--mut:#999;--line:#333;--hd:#1c1c1e;--warn:#ffb340}}}}
body{{background:var(--bg);color:var(--fg);font:14px/1.4 -apple-system,Segoe UI,Roboto,sans-serif;margin:24px}}
h1{{margin:0 0 4px}} .mut{{color:var(--mut)}} table{{border-collapse:collapse;width:100%;margin:8px 0 24px}}
th,td{{border-bottom:1px solid var(--line);padding:4px 8px;text-align:left;vertical-align:top}}
th{{background:var(--hd);position:sticky;top:0;cursor:pointer}} td{{font-size:13px}}
.warn li{{color:var(--warn)}} .chip{{display:inline-block;border:1px solid var(--line);border-radius:10px;padding:1px 8px;margin:2px}}
.wrap{{overflow-x:auto}}</style></head><body>
<h1>{e(site)} network map</h1>
<div class="mut">netscan {VERSION} · {e(scan['when'])} · from {e(scan.get('local_ip') or '?')} ·
{len(rows)} devices · vendors: {e(scan.get('oui_source', ''))}</div>
<p>{''.join(f'<span class="chip">{e(k)}: {v}</span>' for k, v in sorted(roles.items(), key=lambda x: -x[1]))}</p>"""]
    if warnings:
        parts.append("<h2>Check these</h2><ul class=warn>" +
                     "".join(f"<li>{e(w)}</li>" for w in warnings) + "</ul>")
    parts.append("<h2>Networks (networks.csv)</h2><div class=wrap><table><tr>" +
                 "".join(f"<th>{e(c)}</th>" for c in NET_COLS) + "</tr>" +
                 "".join("<tr>" + "".join(f"<td>{e(str(n.get(c, '')))}</td>" for c in NET_COLS) +
                         "</tr>" for n in nets) + "</table></div>")
    for sub, rs in sorted(by_sub.items(), key=lambda x: ip_key(x[0].split("/")[0])):
        parts.append(f"<h2>{e(sub)} <span class=mut>({len(rs)})</span></h2><div class=wrap>"
                     "<table class=sort><tr>" + "".join(f"<th>{e(c)}</th>" for c in cols) + "</tr>")
        for r in sorted(rs, key=lambda r: ip_key(r["ip"])):
            parts.append("<tr>" + "".join(f"<td>{e(str(r.get(c, '')))}</td>" for c in cols) + "</tr>")
        parts.append("</table></div>")
    parts.append("""<script>
document.querySelectorAll('table.sort th').forEach((th,i)=>th.onclick=()=>{
 const t=th.closest('table'),rows=[...t.rows].slice(1),k=r=>r.cells[i].innerText,
 ipn=s=>/^\\d+\\.\\d+\\.\\d+\\.\\d+$/.test(s)?s.split('.').reduce((a,b)=>a*256+ +b,0):null,
 asc=th.dataset.asc!=='1';th.dataset.asc=asc?'1':'0';
 rows.sort((a,b)=>{const x=k(a),y=k(b),nx=ipn(x),ny=ipn(y);
  const c=(nx!==null&&ny!==null)?nx-ny:x.localeCompare(y,undefined,{numeric:true});return asc?c:-c});
 rows.forEach(r=>t.appendChild(r));});
</script></body></html>""")
    with open(path, "w", encoding="utf-8") as f:
        f.write("".join(parts))


# --------------------------------------------------------------------------- main

def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Map a LAN and write a UniFi import plan.")
    ap.add_argument("--site", default="site", help="site name for the report (e.g. 'Sam Stern')")
    ap.add_argument("--subnet", action="append", default=[],
                    help="CIDR to scan (repeatable). Default: this computer's own subnet")
    ap.add_argument("--known", action="append", default=[],
                    help="old IP sheet / CSV (name, ip, mac) to merge names from (repeatable)")
    ap.add_argument("--no-known-subnets", action="store_true",
                    help="don't also scan /24s that only appear in --known lists")
    ap.add_argument("--out", help="output folder (default ./netmap-<site>)")
    ap.add_argument("--threads", type=int, default=128)
    ap.add_argument("--gentle", action="store_true", help="fewer threads, slower timeouts")
    ap.add_argument("--oui", help="IEEE oui.csv to use instead of downloading it")
    ap.add_argument("--offline", action="store_true", help="don't download the vendor list")
    ap.add_argument("--force", action="store_true", help="allow scanning more than 4096 addresses")
    ap.add_argument("--passes", type=int, default=2, help="sweep passes (2nd only re-tries silent addresses)")
    args = ap.parse_args(argv)

    threads = 32 if args.gentle else args.threads
    qto, pto = (1.0, 1.2) if args.gentle else (0.5, 0.8)
    out = args.out or "netmap-" + re.sub(r"[^\w-]+", "-", args.site).strip("-").lower()
    os.makedirs(out, exist_ok=True)

    log(f"netscan {VERSION} on {platform.system()} - output in {os.path.abspath(out)}")
    local_ip, local_net, gateway = local_network()
    log(f"  this computer: {local_ip}  network: {local_net}  gateway: {gateway}")

    known = []
    for k in args.known:
        rows = parse_known(k)
        log(f"  known list {k}: {len(rows)} addresses")
        known += rows
    known_by_ip = {}
    for r in known:
        known_by_ip.setdefault(r["ip"], r)

    subnets = [str(ipaddress.ip_network(s, strict=False)) for s in args.subnet]
    if not subnets and local_net:
        subnets = [local_net]
    if not args.no_known_subnets:
        for r in known:
            n = str(ipaddress.ip_network(r["ip"] + "/24", strict=False))
            if not any(ipaddress.ip_address(r["ip"]) in ipaddress.ip_network(s) for s in subnets):
                subnets.append(n)
                log(f"  + {n} (from the known list: {r['name'] or r['ip']})")
    targets = sorted({h for s in subnets for h in subnet_hosts(s)}, key=ip_key)
    if len(targets) > 4096 and not args.force:
        log(f"refusing to sweep {len(targets)} addresses; narrow --subnet or pass --force")
        return 2
    if not targets:
        log("nothing to scan: pass --subnet")
        return 2

    oui = Oui(args.oui, args.offline)
    t0 = time.time()

    log(f"[1/4] multicast discovery (mDNS, SSDP) on {local_ip}")
    mdns, ssdp = {}, {}
    if local_ip:
        with cf.ThreadPoolExecutor(2) as ex:
            fm, fs = ex.submit(mdns_browse, local_ip), ex.submit(ssdp_discover, local_ip)
            mdns, ssdp = fm.result(), fs.result()
    log(f"  mDNS: {len(mdns)} hosts, SSDP: {len(ssdp)} hosts")

    log(f"[2/4] sweeping {len(targets)} addresses in {', '.join(subnets)}")
    arp: dict[str, str] = {}
    alive = sweep(targets, threads, qto, arp)
    if args.passes > 1:
        silent = [ip for ip in targets if ip not in alive and ip not in arp]
        log(f"  second pass over {len(silent)} silent addresses")
        for ip, how in sweep(silent, threads, qto * 2, arp).items():
            alive[ip] = how
    time.sleep(1.0)  # let the last ARP replies land
    arp.update(arp_table())
    in_scope = set(targets)
    for ip in arp:
        if ip in in_scope:
            alive.setdefault(ip, set()).add("arp")
    for src, tag in [(mdns, "mdns"), (ssdp, "ssdp")]:
        for ip in src:
            if ip in in_scope:
                alive.setdefault(ip, set()).add(tag)
    if local_ip in in_scope:
        alive.setdefault(local_ip, set()).add("self")
    log(f"  {len(alive)} hosts answer ({sum(1 for ip in alive if ip in arp)} with a MAC)")

    log(f"[3/4] probing {len(alive)} hosts: ports, names, banners")
    devs: dict[str, dict] = {}
    done = 0
    with cf.ThreadPoolExecutor(max(8, threads // 4)) as ex:
        for d in ex.map(lambda ip: deep(ip, pto), sorted(alive, key=ip_key)):
            devs[d["ip"]] = d
            done += 1
            if done % 16 == 0 or done == len(alive):
                log(f"  probed {done}/{len(alive)}")

    log("[4/4] UPnP descriptions, vendors, roles")
    upnp_jobs = {ip: s["locations"] for ip, s in ssdp.items() if ip in devs}
    with cf.ThreadPoolExecutor(16) as ex:
        futs = {ex.submit(upnp_description, locs[0]): ip for ip, locs in upnp_jobs.items() if locs}
        for f in cf.as_completed(futs):
            if f.result():
                devs[futs[f]]["upnp"] = f.result()

    now = dt.datetime.now().isoformat(timespec="seconds")
    for ip, d in devs.items():
        d["seen_by"] = sorted(alive.get(ip, set()))
        d["last_seen"] = now
        mac = arp.get(ip) or d.get("netbios", {}).get("mac") or d.get("crestron", {}).get("mac") or ""
        if ip == local_ip:
            d["self"] = True
            mac = mac or ""
        if mac:
            d["mac"] = mac
            d["vendor"] = oui.vendor(mac)
            d["random_mac"] = is_random_mac(mac)
        if ip == gateway:
            d["gateway"] = True
        if ip in mdns:
            d["mdns"] = mdns[ip]
            d["mdns_host"] = d.get("mdns_host") or mdns[ip].get("host", "")
        if ip in ssdp:
            d["ssdp_server"] = ssdp[ip]["server"]
        if "cast" in d and d["cast"].get("name"):
            d.setdefault("upnp", {}).setdefault("friendlyName", d["cast"]["name"])
        k = known_by_ip.get(ip)
        if k:
            d["known_name"], d["known_note"] = k["name"], k["note"]
            if k["mac"] and d.get("mac") and k["mac"] != d["mac"]:
                d["known_mac_mismatch"] = k["mac"]
        d["subnet"] = next((s for s in subnets if ipaddress.ip_address(ip) in ipaddress.ip_network(s)), "")
        d["role"] = classify(d)
        d["best_name"] = best_name(d)

    # merge with an earlier scan in the same folder: keep devices that were off this time
    scan_path = os.path.join(out, "scan.json")
    if os.path.exists(scan_path):
        try:
            with open(scan_path, encoding="utf-8") as f:
                old = json.load(f)
            kept = 0
            for od in old.get("devices", []):
                if od["ip"] not in devs and od["ip"] in in_scope:
                    od.setdefault("seen_by", [])
                    devs[od["ip"]] = od
                    kept += 1
            log(f"  merged {kept} devices seen only in the earlier scan")
        except (OSError, ValueError, KeyError):
            log("  (earlier scan.json unreadable; not merged)")

    # known-list entries nobody answered for
    missing = [k for ip, k in known_by_ip.items() if ip not in devs]

    # warnings
    warnings = []
    macs: dict[str, list[str]] = {}
    for d in devs.values():
        if d.get("mac"):
            macs.setdefault(d["mac"], []).append(d["ip"])
    for mac, ips in macs.items():
        if len(ips) > 1:
            warnings.append(f"{mac} answers on {len(ips)} addresses ({', '.join(sorted(ips, key=ip_key))})"
                            " - a router/proxy-ARP, a multi-IP device, or a stale ARP entry")
    for d in devs.values():
        if d.get("known_mac_mismatch"):
            warnings.append(f"{d['ip']} {d.get('known_name')}: old sheet says MAC {d['known_mac_mismatch']},"
                            f" the network says {d['mac']} - replaced device or wrong sheet")
    if missing:
        warnings.append(f"{len(missing)} addresses from the old sheet did not answer (off, unplugged, "
                        f"or on a network this laptop can't reach): " +
                        ", ".join(f"{k['ip']} {k['name']}".strip() for k in
                                  sorted(missing, key=lambda k: ip_key(k["ip"]))[:40]))
    routed = [d["ip"] for d in devs.values() if not d.get("mac") and not d.get("self")]
    if routed:
        warnings.append(f"{len(routed)} hosts have no MAC (routed subnet, or ARP not readable): "
                        "they cannot be reserved until the MAC is filled in by hand")
    rnd = [d["ip"] for d in devs.values() if d.get("random_mac")]
    if rnd:
        warnings.append(f"{len(rnd)} private/random MACs (phones, tablets): left un-reserved")

    # networks.csv: suggested once, then the user's file wins
    net_path = os.path.join(out, "networks.csv")
    net_rows = read_csv(net_path)
    net_name_of: dict[str, str] = {}
    for n in net_rows:
        try:
            net_name_of[str(ipaddress.ip_interface(n["gateway_cidr"]).network)] = n["name"]
        except (ValueError, KeyError):
            pass
    prev_by_mac, prev_by_ip = {}, {}
    for r in read_csv(os.path.join(out, "devices.csv")):
        if r.get("mac"):
            prev_by_mac[r["mac"]] = r
        else:
            prev_by_ip[r.get("ip")] = r
    new_nets = []
    for s in subnets:
        if s in net_name_of:
            continue
        net = ipaddress.ip_network(s)
        has = [d for d in devs.values() if d.get("subnet") == s and not d.get("self")]
        if not has and s != local_net:
            continue
        gw = gateway if gateway and ipaddress.ip_address(gateway) in net else str(next(net.hosts()))
        name = "Default" if s == local_net else f"LAN {net.network_address}"
        net_name_of[s] = name
        new_nets.append({"name": name, "vlan": "", "gateway_cidr": f"{gw}/{net.prefixlen}",
                         "dns1": "", "dns2": "", "domain": "", "scanned_from": s,
                         "notes": "this laptop's network" if s == local_net else "routed / from the old sheet"})

    rows = []
    for d in sorted(devs.values(), key=lambda d: ip_key(d["ip"])):
        r = device_row(d, net_name_of)
        prev = (r["mac"] and prev_by_mac.get(r["mac"])) or prev_by_ip.get(r["ip"])
        if prev:
            for c in PLAN_COLS:
                if c in prev and prev[c] is not None:
                    r[c] = prev[c]
        rows.append(r)
    for n in new_nets:
        reserved = {r["fixed_ip"] for r in rows if r["reserve"].upper().startswith("Y")}
        n["dhcp_start"], n["dhcp_stop"], why = suggest_dhcp(n["scanned_from"],
                                                            n["gateway_cidr"].split("/")[0], reserved)
        if why:
            n["notes"] = (n["notes"] + "; " + why).strip("; ")
    net_rows += new_nets

    scan = {"tool": f"netscan {VERSION}", "site": args.site, "when": now, "local_ip": local_ip,
            "local_net": local_net, "gateway": gateway, "subnets": subnets,
            "oui_source": oui.source, "seconds": round(time.time() - t0),
            "devices": sorted(devs.values(), key=lambda d: ip_key(d["ip"])),
            "known_missing": missing, "warnings": warnings}
    with open(scan_path + ".tmp", "w", encoding="utf-8") as f:
        json.dump(scan, f, indent=1, default=lambda o: sorted(o) if isinstance(o, set) else str(o))
    os.replace(scan_path + ".tmp", scan_path)
    write_csv(os.path.join(out, "devices.csv"), DEV_COLS, rows)
    write_csv(net_path, NET_COLS, net_rows)
    write_report(os.path.join(out, "report.html"), args.site, scan, rows, net_rows, warnings)

    n_res = sum(1 for r in rows if r["reserve"].upper().startswith("Y"))
    log(f"\ndone in {scan['seconds']} s: {len(rows)} devices, {n_res} marked for a reservation, "
        f"{len(net_rows)} networks")
    for w in warnings:
        log(f"  ! {w}")
    log(f"\nNext: open {os.path.join(out, 'report.html')}, fix devices.csv / networks.csv in Excel,\n"
        f"then: python3 unifi_import.py --plan {out} --host <UDM address>   (dry run first)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
