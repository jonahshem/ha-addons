"""Get the house-commissioning integration onto this box, then get out of the way.

Home Assistant can install an add-on from a repository URL, which is one paste
in the UI. It has no equivalent for a custom integration - there is no store for
those - so the only way to put one on a box without SSH, Samba or HACS is an
add-on that writes it. That is the whole of this add-on.

Everything that actually commissions the house is in the integration this
installs: it asks for the credentials, makes the Cloudflare tunnel, installs and
configures the other add-ons, and sets Crestron Home up. Doing it there rather
than here means an imaged house - which never installs this add-on at all -
runs exactly the same code.
"""
import json
import os
import shutil
import ssl
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

RAW = "https://raw.githubusercontent.com/jonahshem/ha-addons/main"
MANIFEST_URL = f"{RAW}/custom_components/versions.json"
INTEGRATIONS = ("bav_house", "crestron_home")
PORT = 8097
STATE_PATH = "/data/state.json"

# 🔴 With `map: homeassistant_config`, Home Assistant's configuration directory
# is mounted at /homeassistant, and /config is this add-on's OWN private folder.
# Writing custom_components to /config would succeed, be invisible to Home
# Assistant, and look exactly like a bug in the integration. /config is only the
# config directory on Supervisors old enough to predate the rename.
CONFIG_CANDIDATES = ("/homeassistant", "/config")

SUPERVISOR_URLS = ("http://supervisor", "http://172.30.32.2")

STATE = {"running": False, "done": False, "error": None, "lines": [],
         "config_dir": None, "versions": {}}
LOCK = threading.Lock()


def log(msg):
    line = f"{time.strftime('%H:%M:%S')}  {msg}"
    print(line, flush=True)
    STATE["lines"].append(line)
    del STATE["lines"][:-60]


def options():
    try:
        with open(os.environ.get("CONF") or "/data/options.json", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def config_dir():
    """Home Assistant's configuration directory, as this container sees it."""
    for path in CONFIG_CANDIDATES:
        if os.path.isdir(path) and os.path.exists(os.path.join(path, "configuration.yaml")):
            return path
    # A box whose configuration.yaml has not been written yet (Core has never
    # started) still has the directory - take it, but only the mapped one.
    for path in CONFIG_CANDIDATES:
        if os.path.isdir(path) and os.access(path, os.W_OK):
            return path
    return None


def get(url, timeout=60):
    ctx = ssl.create_default_context()
    req = urllib.request.Request(url, headers={"User-Agent": "bavsetup"})
    with urllib.request.urlopen(req, timeout=timeout, context=ctx) as resp:
        return resp.read()


def install_integration(manifest, domain, target_root):
    """Write one integration's files, all-or-nothing.

    Downloaded in full before anything lands: a half-written integration stops
    Home Assistant from starting, and a house that will not start is a call-out.
    """
    entry = (manifest.get("integrations") or {}).get(domain)
    if not entry:
        raise RuntimeError(f"{domain} is not listed in versions.json")
    base = manifest.get("base") or f"{RAW}/custom_components"
    files = entry.get("files") or []
    if not files:
        raise RuntimeError(f"{domain} lists no files")
    blobs = {name: get(f"{base}/{domain}/{name}") for name in files}

    target = os.path.join(target_root, "custom_components", domain)
    staging = target + ".new"
    shutil.rmtree(staging, ignore_errors=True)
    os.makedirs(staging, exist_ok=True)
    try:
        for name, blob in blobs.items():
            path = os.path.join(staging, name)
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "wb") as f:
                f.write(blob)
        os.makedirs(target, exist_ok=True)
        for name in blobs:
            dst = os.path.join(target, name)
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            os.replace(os.path.join(staging, name), dst)
    finally:
        shutil.rmtree(staging, ignore_errors=True)
    version = entry.get("version") or "?"
    STATE["versions"][domain] = version
    log(f"{domain} {version}: {len(blobs)} files -> {target}")
    return version


def add_boot_hook(target_root):
    """Add `bav_house:` to configuration.yaml so it loads on every boot.

    Without it Home Assistant never imports an integration that has no config
    entry, so a box that has not been commissioned yet would show nothing at
    all. Appended, never rewritten, and only if it is not already there.
    """
    path = os.path.join(target_root, "configuration.yaml")
    try:
        with open(path, encoding="utf-8") as f:
            text = f.read()
    except FileNotFoundError:
        text = ""
    for line in text.splitlines():
        if line.strip().rstrip(":") == "bav_house" and not line.startswith((" ", "\t")):
            log("configuration.yaml already loads bav_house")
            return False
    if text:
        shutil.copy2(path, path + ".bavsetup.bak")
    block = ("\n# Added by the BAV House Setup add-on: loads the commissioning\n"
             "# integration on every boot, so a house that has not been set up\n"
             "# offers to set itself up. Safe to remove once commissioned.\n"
             "bav_house:\n")
    with open(path, "a", encoding="utf-8") as f:
        f.write(block if text.endswith("\n") or not text else "\n" + block)
    log("added `bav_house:` to configuration.yaml")
    return True


def supervisor(method, path, body=None, timeout=60):
    token = os.environ.get("SUPERVISOR_TOKEN") or os.environ.get("HASSIO_TOKEN")
    if not token:
        raise RuntimeError("no SUPERVISOR_TOKEN - run.sh must use with-contenv")
    last = None
    for base in SUPERVISOR_URLS:
        req = urllib.request.Request(
            base + path, method=method,
            data=json.dumps(body).encode() if body is not None else None,
            headers={"Authorization": f"Bearer {token}",
                     "Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return json.load(resp)
        except urllib.error.HTTPError as e:
            raise RuntimeError(
                f"{method} {path}: {e.code} "
                f"{e.read()[:200].decode('utf-8', 'replace')}") from e
        except Exception as e:  # noqa: BLE001 - try the other address
            last = e
    raise RuntimeError(f"could not reach the Supervisor: {last!r}")


def run(opts):
    with LOCK:
        if STATE["running"]:
            return
        STATE["running"] = True
    STATE["error"] = None
    try:
        root = config_dir()
        STATE["config_dir"] = root
        if not root:
            raise RuntimeError(
                "Home Assistant's config directory is not mapped into this "
                "add-on - expected /homeassistant")
        log(f"config directory: {root}")
        manifest = json.loads(get(MANIFEST_URL).decode("utf-8"))
        log(f"versions.json: {', '.join(sorted(manifest.get('integrations') or {}))}")
        for domain in INTEGRATIONS:
            if domain in (manifest.get("integrations") or {}):
                install_integration(manifest, domain, root)
            else:
                log(f"{domain}: not published yet, skipped")
        if opts.get("start_on_boot", True):
            add_boot_hook(root)
        STATE["done"] = True
        if opts.get("restart_after", True):
            log("restarting Home Assistant - a custom integration is only "
                "imported at start")
            supervisor("POST", "/core/restart", timeout=300)
            log("restart requested")
        else:
            log("restart Home Assistant to finish, then open "
                "Settings > Devices & services")
    except Exception as e:  # noqa: BLE001 - this page is the only place it would be seen
        STATE["error"] = f"{type(e).__name__}: {e}"
        log(f"FAILED: {STATE['error']}")
    finally:
        STATE["running"] = False


PAGE = """<!doctype html><meta charset="utf-8"><title>BAV House Setup</title>
<style>
body{font:14px system-ui,sans-serif;margin:1.2rem;color:#222;background:#fafafa;max-width:52rem}
h1{font-size:1.2rem;margin:0 0 .2rem} p{margin:.4rem 0}
button{padding:.4rem .9rem;margin-right:.4rem;border:1px solid #888;border-radius:.3rem;background:#fff;cursor:pointer}
pre{background:#f0f0f0;padding:.6rem;overflow:auto;max-height:22rem;white-space:pre-wrap}
.ok{color:#2a7} .bad{color:#c33} .muted{color:#777}
</style>
<h1>BAV House Setup</h1>
<p class="muted">Puts the commissioning integration on this box. Everything else
- the Cloudflare tunnel, the other add-ons, Crestron Home - is asked for and
done by that integration, in Settings &rsaquo; Devices &amp; services.</p>
<p><button onclick="go()">Install now</button><span id="msg" class="muted"></span></p>
<div id="next"></div>
<pre id="log"></pre>
<script>
async function load(){
  const r=await fetch('state',{cache:'no-store'}); if(!r.ok)return;
  const d=await r.json();
  document.getElementById('log').textContent=d.lines.join('\\n');
  const m=document.getElementById('msg');
  m.textContent=d.running?' working…':(d.error?' failed':(d.done?' done':''));
  m.className=d.error?'bad':(d.done?'ok':'muted');
  document.getElementById('next').innerHTML=(d.done&&!d.error)
    ? '<p class="ok">Installed '+Object.entries(d.versions).map(([k,v])=>k+' '+v).join(', ')
      +'. Once Home Assistant has restarted, open <b>Settings &rsaquo; Devices &amp; services</b>'
      +' and set up <b>BAV House Setup</b>.</p>'
    : (d.error?'<p class="bad">'+d.error+'</p>':'');
}
async function go(){await fetch('run',{method:'POST'});load();}
load();setInterval(load,2000);
</script>"""


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _tail(self):
        path = urlparse(self.path).path.rstrip("/")
        return "/" + path.rsplit("/", 1)[-1] if path else "/"

    def _send(self, body, ctype):
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        tail = self._tail()
        if tail == "/state":
            self._send(json.dumps(STATE).encode(), "application/json")
        else:
            self._send(PAGE.encode(), "text/html; charset=utf-8")

    def do_POST(self):
        if self._tail() == "/run":
            threading.Thread(target=run, args=(options(),), daemon=True).start()
        self._send(json.dumps({"started": True}).encode(), "application/json")


if __name__ == "__main__":
    opts = options()
    if opts.get("run_on_start", True):
        threading.Thread(target=run, args=(opts,), daemon=True).start()
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()
