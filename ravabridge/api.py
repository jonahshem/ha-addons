"""A small HTTP face on the bridge, for Home Assistant and HomeUI.

    GET  /                  a status page (this is what Home Assistant's ingress opens)
    GET  /health            up, address, live calls
    GET  /status            everything: panels, doors, registrations, calls, last events
    POST /discover          find panels and doors now; what is found lands in the options
    POST /ring?door=NAME    ring that door's panels with no media, to prove the wiring
    POST /hangup            end every call
    POST /unlock?door=NAME  open that door through UniFi Access (needs `access` + the door's id)
    POST /protectprobe?camera=NAME  pull a Protect camera's media only - proves it flows, rings nothing
    POST /protectsave       {cameras:[...]} from the page: call, triggers, ring, talkback, quality

Reachable through Home Assistant's ingress, or on the LAN with the bearer
token. The token defaults to the PIN this dealer already uses, so a fresh
install works without a lookup; change it per site if that PIN is not yours.

Paths are matched on their last segment: ingress may or may not leave its
prefix on, and the page's own links are relative so they work either way.
"""
import json
import ssl
import threading
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

import discover
import sip

BRIDGE = None
CFG = {}
LOG = print
DISCOVER_LOCK = threading.Lock()
LAST_DISCOVERY = {"at": None, "report": None}

PAGE = """<!doctype html><meta charset="utf-8"><title>Rava Bridge</title>
<style>
body{font:14px system-ui,sans-serif;margin:1.2rem;color:#222;background:#fafafa}
h1{font-size:1.2rem;margin:0 0 .6rem} h2{font-size:1rem;margin:1.2rem 0 .4rem}
table{border-collapse:collapse;min-width:24rem} td,th{padding:.25rem .6rem;border-bottom:1px solid #e3e3e3;text-align:left}
button{padding:.35rem .8rem;margin-right:.4rem;border:1px solid #888;border-radius:.3rem;background:#fff;cursor:pointer}
.ok{color:#2a7} .bad{color:#c33} .muted{color:#777} pre{background:#f0f0f0;padding:.6rem;overflow:auto;max-height:16rem}
</style>
<h1>Rava Bridge <span id="addr" class="muted"></span></h1>
<div><button onclick="act('discover')">Discover panels & doors</button><button onclick="act('ring')">Test ring</button><button onclick="act('hangup')">Hang up</button> <span id="msg" class="muted"></span></div>
<h2>Panels</h2><table id="panels"></table>
<h2>Doors</h2><table id="doors"></table>
<h2>Calls</h2><table id="calls"></table>
<h2>Protect cameras <span id="pmsg" class="muted"></span></h2>
<div id="protect"><span class="muted">No Protect console configured.</span></div>
<h2>Reader setup</h2><div id="setup" class="muted">Run Discover with the UniFi Access token set to see what to type into the Access app.</div>
<h2>Log</h2><pre id="events"></pre>
<script>
const esc=s=>String(s??'').replace(/[&<>]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;'}[c]));
async function load(){
  const r=await fetch('status',{cache:'no-store'}); if(!r.ok){document.getElementById('msg').textContent='status '+r.status;return;}
  const d=await r.json();
  document.getElementById('addr').textContent=d.address+' · up '+Math.floor(d.uptime/60)+' min';
  document.getElementById('panels').innerHTML='<tr><th>Name</th><th>Host</th><th>Ext</th><th>Groups</th></tr>'+d.panels.map(p=>`<tr><td>${esc(p.name)}</td><td>${esc(p.host)}</td><td>${esc(p.ext)}</td><td>${esc((p.groups||[]).join(', '))}</td></tr>`).join('')||'<tr><td class="muted">none yet</td></tr>';
  document.getElementById('doors').innerHTML='<tr><th>Name</th><th>User</th><th>Registered</th><th>Rings</th><th>Unlock</th></tr>'+d.doors.map(x=>{const r=d.registered[x.user];return `<tr><td>${esc(x.name)}</td><td>${esc(x.user)}</td><td>${r?'<span class=ok>yes, from '+esc(r.from)+' ('+r.expiresIn+' s)</span>':'<span class=bad>no</span>'}</td><td>${esc((x.ring||[]).join(', '))}</td><td>${x.doorId?'<button onclick="act(\\'unlock?door='+encodeURIComponent(x.name)+'\\')">Unlock</button>':'<span class=muted>no door id</span>'}</td></tr>`}).join('')||'<tr><td class="muted">none yet</td></tr>';
  document.getElementById('calls').innerHTML='<tr><th>Door</th><th>State</th><th>Answered by</th><th>Video in/out</th><th>Legs</th></tr>'+d.calls.slice(-5).reverse().map(c=>`<tr><td>${esc(c.door)}</td><td>${esc(c.state)}${c.why?' · '+esc(c.why):''}</td><td>${esc(c.answeredBy||'')}</td><td>${c.videoIn}/${c.videoOut}</td><td>${esc((c.legs||[]).map(l=>l.panel+': '+l.state).join(', '))}</td></tr>`).join('')||'<tr><td class="muted">none</td></tr>';
  document.getElementById('events').textContent=d.events.map(e=>new Date(e.t*1000).toLocaleTimeString()+'  '+e.text).join('\\n');
  if(d.discovery&&d.discovery.readerSetup){const s=d.discovery.readerSetup;document.getElementById('setup').innerHTML='In the Access app: '+esc(s.where)+'<br>Server <b>'+esc(s.server)+'</b>, port <b>'+s.port+'</b>, '+esc(s.transport)+'.<br>'+s.accounts.map(a=>'Reader <b>'+esc(a.reader)+'</b>: user <b>'+esc(a.user)+'</b>, password <b>'+esc(a.password)+'</b>').join('<br>');}
}
async function act(p){document.getElementById('msg').textContent='…';const r=await fetch(p,{method:'POST'});let t;try{t=await r.json()}catch(e){t={status:r.status}}
  document.getElementById('msg').textContent=JSON.stringify(t).slice(0,240);load();}

const TRIG_HELP={ring:'the doorbell button',motion:'any motion',line:'crossing a line',loiter:'loitering'};
let PCAMS=null;
async function loadProtect(){
  const r=await fetch('protect',{cache:'no-store'}); if(!r.ok)return;
  const d=await r.json(); const el=document.getElementById('protect');
  if(!d.enabled){el.innerHTML='<span class="muted">No Protect console configured. Set <b>protect.host</b> and <b>protect.api_key</b> in the add-on Configuration tab.</span>';return;}
  if(PCAMS)return;                       // do not clobber edits in progress
  PCAMS=d.cameras;
  const row=c=>{
    const opts=(c.available||[]).map(t=>`<option value="${esc(t)}"${(c.triggers||[]).includes(t)?' selected':''}>${esc(t)}${TRIG_HELP[t]?' — '+TRIG_HELP[t]:''}</option>`).join('');
    const q=['low','medium','high'].map(v=>`<option value="${v}"${c.quality===v?' selected':''}>${v}</option>`).join('');
    return `<tr data-id="${esc(c.camera_id)}">
      <td><b>${esc(c.name)}</b><br><span class="muted">${esc(c.type||'')}</span></td>
      <td><input type="checkbox" class="c-call"${c.call?' checked':''}></td>
      <td><select class="c-trig" multiple size="4" style="min-width:13rem">${opts}</select></td>
      <td><input class="c-ring" value="${esc((c.ring||[]).join(', '))}" size="12"></td>
      <td><input type="checkbox" class="c-talk"${c.talkback?' checked':''}></td>
      <td>${c.has_face?`<input type="checkbox" class="c-face"${c.pause_face?' checked':''}>`:'<span class="muted">—</span>'}</td>
      <td><select class="c-q">${q}</select></td>
      <td><button onclick="probe(this,'${esc(c.name)}')">Test media</button>
          <button onclick="ringCam('${esc(c.name)}')" title="This RINGS every panel">Ring…</button></td></tr>`;
  };
  el.innerHTML='<table><tr><th>Camera</th><th>Call</th><th>Triggers (ctrl-click for more)</th><th>Rings</th><th>Talkback</th><th title="Stop the camera recognising faces while a call is up">Pause face</th><th>Quality</th><th></th></tr>'
    +d.cameras.map(row).join('')+'</table>'
    +'<div style="margin-top:.5rem"><button onclick="saveProtect()">Save cameras</button> '
    +'<span class="muted">“Test media” pulls the picture and sound only — it rings nothing. “Ring…” places a real call to the panels.</span></div>';
}
function saveProtect(){
  const rows=[...document.querySelectorAll('#protect tr[data-id]')].map(tr=>({
    camera_id:tr.dataset.id,
    call:tr.querySelector('.c-call').checked,
    triggers:[...tr.querySelector('.c-trig').selectedOptions].map(o=>o.value),
    ring:tr.querySelector('.c-ring').value.split(',').map(x=>x.trim()).filter(Boolean),
    talkback:tr.querySelector('.c-talk').checked,
    pause_face:tr.querySelector('.c-face')?tr.querySelector('.c-face').checked:true,
    quality:tr.querySelector('.c-q').value}));
  document.getElementById('pmsg').textContent='saving…';
  fetch('protectsave',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({cameras:rows})})
    .then(r=>r.json()).then(t=>{document.getElementById('pmsg').textContent=t.error?('error: '+t.error):('saved '+t.saved+' camera(s)'+(t.persisted?'':' (in memory only)'));PCAMS=null;loadProtect();});
}
function probe(btn,name){
  const was=btn.textContent; btn.textContent='…'; btn.disabled=true;
  fetch('protectprobe?seconds=8&camera='+encodeURIComponent(name),{method:'POST'}).then(r=>r.json()).then(d=>{
    btn.textContent=was; btn.disabled=false;
    document.getElementById('pmsg').textContent = d.error ? (name+': '+d.error)
      : `${name}: picture ${d.video.startedAfter}s ${d.video.perSecond}/s, sound ${d.audio.startedAfter}s ${d.audio.perSecond}/s ${d.codec}${d.audioSteady?'':' (audio not steady)'} — nothing rang`;
  });
}
function ringCam(name){
  if(!confirm('This RINGS every panel in '+name+"'s list. Continue?"))return;
  fetch('protectring?camera='+encodeURIComponent(name),{method:'POST'}).then(r=>r.json())
    .then(t=>{document.getElementById('pmsg').textContent=JSON.stringify(t).slice(0,200);});
}
load(); setInterval(load,4000); loadProtect(); setInterval(loadProtect,15000);
</script>
"""


class Handler(BaseHTTPRequestHandler):
    server_version = "RavaBridge/0.2"

    def log_message(self, fmt, *args):
        pass

    def _json(self, obj, code=200):
        body = json.dumps(obj, indent=1).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _html(self, text):
        body = text.encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _allowed(self):
        # Through ingress the Supervisor has already authenticated the caller
        # and stamps X-Ingress-Path; anything arriving on the LAN port must
        # carry the token.
        if self.headers.get("X-Ingress-Path"):
            return True
        token = str(CFG.get("api_token") or "")
        if not token:
            return True
        auth = self.headers.get("Authorization") or ""
        q = parse_qs(urlparse(self.path).query)
        return auth == f"Bearer {token}" or (q.get("token") or [""])[0] == token

    def _body(self, cap=1 << 20):
        try:
            n = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            return b""
        return self.rfile.read(min(n, cap)) if n > 0 else b""

    def _tail(self):
        path = urlparse(self.path).path.rstrip("/")
        return "/" + path.rsplit("/", 1)[-1] if path else "/"

    def do_GET(self):
        path = self._tail()
        if path in ("/", "/index.html"):
            self._html(PAGE)
            return
        if path == "/health":
            st = BRIDGE.status()
            live = [c for c in st["calls"] if c["state"] != "done"]
            self._json({"ok": True, "address": st["address"], "uptime": st["uptime"],
                        "panels": len(st["panels"]), "registered": list(st["registered"]), "calls": live})
            return
        if not self._allowed():
            self._json({"error": "unauthorized"}, 401)
            return
        if path == "/pages":
            pl = getattr(BRIDGE, "pages", None)
            self._json(pl.public() if pl else {"error": "page listening is off"})
            return
        if path == "/status":
            st = BRIDGE.status()
            st["discovery"] = {"at": LAST_DISCOVERY["at"],
                               "readerSetup": (LAST_DISCOVERY["report"] or {}).get("readerSetup"),
                               "error": (LAST_DISCOVERY["report"] or {}).get("error"),
                               "supervisor": dict(discover.SUPERVISOR_STATE)}
            crpc_mgr = getattr(BRIDGE, "crpc", None)
            st["crpc"] = crpc_mgr.public() if crpc_mgr else None
            pd = getattr(BRIDGE, "protect", None)
            st["protect"] = pd.public() if pd else None
            self._json(st)
            return
        if path == "/protect":
            pd = getattr(BRIDGE, "protect", None)
            self._json(pd.public() if pd else {"error": "no Protect console configured"})
            return
        self._json({"error": "not found"}, 404)

    def do_POST(self):
        if not self._allowed():
            self._json({"error": "unauthorized"}, 401)
            return
        u = urlparse(self.path)
        path = self._tail()
        q = parse_qs(u.query)
        if path == "/hangup":
            self._json({"ended": BRIDGE.hangup_all()})
            return
        if path == "/discover":
            self._json(run_discovery(reason="requested"))
            return
        if path == "/ring":
            name = (q.get("door") or [""])[0]
            door = next((d for d in BRIDGE.doors.values() if d.name.lower() == name.lower() or d.user == name), None)
            if door is None and BRIDGE.doors:
                door = next(iter(BRIDGE.doors.values()))
            if door is None:
                self._json({"error": "no doors configured - run Discover, or add one"}, 400)
                return
            seconds = int((q.get("seconds") or ["8"])[0])
            self._json({"ringing": door.name, "seconds": seconds, "call": test_ring(door, seconds)})
            return
        if path == "/unlock":
            self._json(unlock((q.get("door") or [""])[0]))
            return
        if path == "/protectsave":
            pd = getattr(BRIDGE, "protect", None)
            if not pd or not pd.enabled:
                self._json({"error": "no Protect console configured"}, 400)
                return
            try:
                body = json.loads(self._body().decode("utf-8") or "{}")
            except ValueError as e:
                self._json({"error": f"that was not JSON: {e}"}, 400)
                return
            self._json(pd.save_settings(body.get("cameras") or []))
            return
        if path == "/protectprobe":         # media only: pulls the camera, rings nothing
            pd = getattr(BRIDGE, "protect", None)
            if not pd or not pd.enabled:
                self._json({"error": "no Protect console configured"}, 400)
                return
            which = (q.get("camera") or q.get("door") or q.get("name") or [""])[0]
            if not which and pd.cameras:
                which = pd.cameras[0]["name"]
            secs = (q.get("seconds") or ["8"])[0]
            try:
                secs = int(secs)
            except ValueError:
                secs = 8
            self._json(pd.probe(which, secs, (q.get("quality") or [""])[0] or None,
                                (q.get("codec") or [""])[0] or None))
            return
        if path == "/protectring":         # one path segment: _tail() keeps only the last
            pd = getattr(BRIDGE, "protect", None)
            if not pd or not pd.enabled:
                self._json({"error": "no Protect console configured"}, 400)
                return
            which = (q.get("camera") or q.get("door") or q.get("name") or [""])[0]
            if not which and pd.cameras:
                which = next((e["name"] for e in pd.cameras if e["call"]), pd.cameras[0]["name"])
            ok = pd.ring_now(which)
            self._json({"ringing": which} if ok else {"error": f"no Protect camera called {which!r}"},
                       200 if ok else 404)
            return
        self._json({"error": "not found"}, 404)


def run_discovery(reason=""):
    if not DISCOVER_LOCK.acquire(blocking=False):
        return {"error": "discovery already running"}
    try:
        report = discover.run(CFG, BRIDGE.address, log=LOG, apply=apply_options)
        LAST_DISCOVERY["at"] = time.time()
        LAST_DISCOVERY["report"] = report
        return report
    except Exception as e:
        LOG(f"discover: {e!r}")
        return {"error": repr(e)}
    finally:
        DISCOVER_LOCK.release()


def apply_options(options):
    global CFG
    CFG = options
    BRIDGE.apply(options)


def test_ring(door, seconds):
    """Ring the door's panels as if the door had called, with no media behind
    it. The panels show the door's name and ring; an answer hears silence. It
    proves the addressing, not the picture."""
    b = BRIDGE
    body = (f"v=0\r\no=test 1 1 IN IP4 {b.address}\r\ns=test\r\nc=IN IP4 {b.address}\r\nt=0 0\r\n"
            f"m=audio 4000 RTP/AVP 0\r\na=rtpmap:0 PCMU/8000\r\n").encode()
    headers = {"via": [f"SIP/2.0/UDP {b.address}:{b.port};branch={sip.new_branch()}"],
               "from": f'"{door.name}" <sip:{door.user}@{b.address}>;tag={sip.new_tag()}',
               "to": f"<sip:test@{b.address}>", "call-id": sip.new_call_id("ravabridge-test"),
               "cseq": "1 INVITE", "contact": f"<sip:{door.user}@{b.address}:{b.port}>",
               "content-type": "application/sdp"}
    msg = sip.Message(method="INVITE", uri=f"sip:test@{b.address}", headers=headers, body=body)
    with b.lock:
        # The bridge's own address is trusted as a door for this one call: the
        # replies land on our SIP port and are ignored, since no leg owns them.
        saved = door.host
        door.host = b.address
        try:
            b._on_invite(msg, (b.address, b.port))
        finally:
            door.host = saved
        call = b.calls.get(msg.call_id)
    if call is None:
        return None

    def stop():
        time.sleep(seconds)
        with b.lock:
            if call.state != "done":
                b._finish(call, "test ring over")
    threading.Thread(target=stop, daemon=True).start()
    return call.public()


def unlock(door_name=""):
    acc = CFG.get("access") or {}
    host, token = acc.get("host"), acc.get("token")
    door = next((d for d in BRIDGE.doors.values() if d.name.lower() == door_name.lower() or d.user == door_name), None)
    door_id = (door.door_id if door else "") or acc.get("door_id")
    if not (host and token):
        return {"ok": False, "error": "set access.host and access.token in the options"}
    if not door_id:
        return {"ok": False, "error": "no Access door id for that door - run Discover, or set door_id"}
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    req = urllib.request.Request(f"https://{host}:12445/api/v1/developer/doors/{door_id}/unlock",
                                 method="PUT", headers={"Authorization": f"Bearer {token}"})
    try:
        with urllib.request.urlopen(req, timeout=10, context=ctx) as r:
            out = json.load(r)
        BRIDGE.note(f"unlocked {door.name if door else door_id} via UniFi Access: {out.get('code')}")
        return {"ok": out.get("code") == "SUCCESS", "response": out}
    except Exception as e:
        BRIDGE.note(f"unlock failed: {e!r}")
        return {"ok": False, "error": repr(e)}


def serve(bridge, cfg, log=print):
    global BRIDGE, CFG, LOG
    BRIDGE, CFG, LOG = bridge, cfg, log
    port = int(cfg.get("api_port") or 8098)
    srv = ThreadingHTTPServer(("0.0.0.0", port), Handler)
    log(f"api on :{port}")
    srv.serve_forever()
