"""Stored announcements: the ten (or however many) clips a house keeps ready.

One folder is the source of truth - `/share/nax-announcements` - and there are
three ways a WAV gets into it:

  * **recorded** in HomeUI and saved, which is the same recorder that already
    posts to `/announce`, just kept instead of thrown away;
  * **dropped in** over Samba or SSH, for anything produced elsewhere;
  * **spoken** from typed text by `/clips` with a `text=`, which is how a
    Crestron keypad ends up announcing "the pool gate is open" without anybody
    ever holding a microphone.

They are the same thing once they land, which is the point: the Crestron driver
asks for clip `pool-gate`, and it does not care which of the three made it.

**Why a folder and not a database.** A house that can look at its announcements
in a file browser, copy one to another house, or delete one by hand, is a house
somebody can fix without this add-on running. The id is just the filename stem,
so the folder listing *is* the API response.

**Ordering is deliberate.** `list_clips` sorts by name, so slot 1 in the driver
means the same clip tomorrow. Sorting by mtime would quietly renumber every
slot the moment somebody re-records one.
"""
import json
import os
import re
import subprocess
import unicodedata

CLIPS_DIR = os.environ.get("CLIPS_DIR", "/share/nax-announcements")
# The set every house starts with, shipped in the image. Seeded into
# CLIPS_DIR once; a clip a person then deletes stays deleted (SEEDED_FILE
# remembers what was seeded, so it is never put back).
BUNDLED_DIR = os.environ.get("BUNDLED_DIR", "/announcements")
SEEDED_FILE = os.environ.get("SEEDED_FILE", "/data/seeded-announcements.json")

# A page, not a broadcast - the same ceiling `/announce` uses.
MAX_SECONDS = 120
MAX_BYTES = 8 * 1024 * 1024
MAX_TEXT = 500

# 48 kHz / 24-bit / stereo, matching what the sender puts on the wire.
BYTES_PER_SECOND = 48000 * 2 * 3


class ClipError(Exception):
    """Something the caller can fix, and should be told in one sentence."""


def _slug(name):
    """A filename that survives Samba, a URL, and being read aloud in support."""
    name = unicodedata.normalize("NFKD", name or "").encode("ascii", "ignore").decode()
    name = re.sub(r"[^A-Za-z0-9]+", "-", name).strip("-").lower()
    return name[:48]


def ensure_dir():
    os.makedirs(CLIPS_DIR, exist_ok=True)
    return CLIPS_DIR


def _meta_path(clip_id):
    return os.path.join(CLIPS_DIR, clip_id + ".json")


def _read_meta(clip_id):
    try:
        with open(_meta_path(clip_id)) as fh:
            return json.load(fh)
    except Exception:
        return {}


def _write_meta(clip_id, **fields):
    try:
        with open(_meta_path(clip_id), "w") as fh:
            json.dump(fields, fh)
    except Exception:
        # The audio is what matters; a missing label is a cosmetic loss and
        # never a reason to fail a save.
        pass


def path_for(clip_id):
    """The WAV for an id, or None. Refuses anything that could leave the folder."""
    clip_id = _slug(clip_id)
    if not clip_id:
        return None
    p = os.path.join(CLIPS_DIR, clip_id + ".wav")
    return p if os.path.isfile(p) else None


def content_type(path):
    """What is really in the file: the recorder and the TTS both save mp3
    under a .wav name, and a browser needs the truth to play it."""
    try:
        with open(path, "rb") as fh:
            head = fh.read(4)
    except OSError:
        return "application/octet-stream"
    if head[:4] == b"RIFF":
        return "audio/wav"
    if head[:3] == b"ID3" or (len(head) >= 2 and head[0] == 0xFF and (head[1] & 0xE0) == 0xE0):
        return "audio/mpeg"
    if head[:4] == b"OggS":
        return "audio/ogg"
    if head[:4] == b"\x1aE\xdf\xa3":
        return "audio/webm"
    return "application/octet-stream"


def seed_bundled(bundled_dir=BUNDLED_DIR, seeded_file=SEEDED_FILE, log=print):
    """Copy the shipped set into the house's folder, once each.

    A clip is seeded if it is not in the folder AND was never seeded before:
    the second condition is what keeps a deleted one deleted across restarts
    and updates. A house's own clip with the same id is never overwritten.
    Returns the ids added.
    """
    try:
        names = sorted(os.listdir(bundled_dir))
    except OSError:
        return []
    try:
        with open(seeded_file) as fh:
            seeded = set(json.load(fh))
    except (OSError, ValueError):
        seeded = set()
    ensure_dir()
    added = []
    for fn in names:
        if not fn.lower().endswith(".wav"):
            continue
        clip_id = fn[:-4]
        meta = {}
        try:
            with open(os.path.join(bundled_dir, clip_id + ".json")) as fh:
                meta = json.load(fh) or {}
        except (OSError, ValueError):
            pass
        if path_for(clip_id):
            # Already here. A bundled clip that has never had a sound set
            # takes the shipped setting (a later release may add one); a
            # person's choice - even "none", stored as "" - is a key that
            # exists, and is kept. A clip the house made itself is untouched.
            have = _read_meta(clip_id)
            if have.get("source") == "bundled":
                new = {k: meta[k] for k in ("before", "after", "sound", "text") if k in meta and k not in have}
                if new:
                    _write_meta(clip_id, **dict(have, **new))
                    log(f"[clips] {clip_id}: shipped setting adopted: {new}")
            continue
        if clip_id in seeded:
            continue
        src = os.path.join(bundled_dir, fn)
        tmp = os.path.join(CLIPS_DIR, "." + clip_id + ".part")
        with open(src, "rb") as a, open(tmp, "wb") as b:
            b.write(a.read())
        os.replace(tmp, os.path.join(CLIPS_DIR, fn))
        fields = {"name": meta.get("name") or clip_id.replace("-", " ").title(),
                  "source": "bundled", "user_facing": bool(meta.get("user_facing", True))}
        for key in ("sound", "before", "after", "text"):
            if key in meta:
                fields[key] = meta[key]
        _write_meta(clip_id, **fields)
        added.append(clip_id)
    if added or not seeded:
        seeded |= {fn[:-4] for fn in names if fn.lower().endswith(".wav")}
        try:
            os.makedirs(os.path.dirname(seeded_file), exist_ok=True)
            with open(seeded_file, "w") as fh:
                json.dump(sorted(seeded), fh)
        except OSError as e:
            log(f"[clips] could not remember what was seeded: {e}")
    if added:
        log(f"[clips] seeded {len(added)} bundled announcement(s): {', '.join(added)}")
    return added


def _looks_like_wav(path):
    """True only for a real RIFF/WAVE file, so a duration guess is never made
    from an mp3 that merely has a .wav name."""
    try:
        with open(path, "rb") as fh:
            head = fh.read(12)
        return head[:4] == b"RIFF" and head[8:12] == b"WAVE"
    except Exception:
        return False


def list_clips():
    ensure_dir()
    out = []
    for fn in sorted(os.listdir(CLIPS_DIR)):
        if not fn.lower().endswith(".wav"):
            continue
        clip_id = fn[:-4]
        full = os.path.join(CLIPS_DIR, fn)
        try:
            size = os.path.getsize(full)
        except OSError:
            continue
        meta = _read_meta(clip_id)
        out.append({
            "id": clip_id,
            "name": meta.get("name") or clip_id.replace("-", " ").title(),
            "source": meta.get("source") or "file",
            # Whether this belongs on the end user's tile. A house wants
            # "Dinner is ready" one tap away and does NOT want fourteen security
            # lines - "house is disarmed" on a wall panel is a button nobody
            # should be handed. Absent means True, so clips saved before this
            # existed keep showing.
            "user_facing": bool(meta.get("user_facing", True)),
            # A sound rather than words - a doorbell, a chime - offered as
            # what to play before or after an announcement, never as one.
            "sound": bool(meta.get("sound")),
            # What plays before/after this clip: absent = the house default,
            # "" = nothing, else a clip id (normally one of the sounds).
            "before": meta.get("before"),
            "after": meta.get("after"),
            # What it says, when known, and how it was spoken - for the
            # editor. None until the clip has been typed or transcribed.
            "text": meta.get("text"),
            "voice": meta.get("voice"),
            "speed": meta.get("speed"),
            "bytes": size,
            # Only meaningful for uncompressed PCM. A clip that arrived as mp3
            # (Fish returns nothing else) is a tenth the size for the same
            # length, so dividing by the PCM rate reports a third of a second
            # for a four-second sentence - worse than saying nothing. The real
            # duration is known only once decoded, so anything compressed
            # reports None and the UI can leave the field blank.
            "approx_seconds": (round(size / BYTES_PER_SECOND, 1)
                               if size and _looks_like_wav(full) else None),
        })
    return out


def set_user_facing(clip_id, facing):
    """Show or hide a clip on the end user's tile. Actions & Events sees it either way."""
    clip_id = _slug(clip_id)
    if not path_for(clip_id):
        raise ClipError("No such clip")
    meta = _read_meta(clip_id)
    meta["user_facing"] = bool(facing)
    _write_meta(clip_id, **meta)
    return clip_id


def set_chime(clip_id, before=None, after=None):
    """What plays around one clip. None leaves a side alone; "default" clears
    it back to the house setting; "none" or "" means nothing; else a clip id."""
    clip_id = _slug(clip_id)
    if not path_for(clip_id):
        raise ClipError("No such clip")
    meta = _read_meta(clip_id)
    for key, val in (("before", before), ("after", after)):
        if val is None:
            continue
        val = str(val).strip()
        if val.lower() == "default":
            meta.pop(key, None)
        elif val.lower() in ("", "none"):
            meta[key] = ""
        else:
            if not path_for(_slug(val)):
                raise ClipError(f"No clip called {val!r} to play {key}")
            meta[key] = _slug(val)
    _write_meta(clip_id, **meta)
    return meta


def resolve_chimes(clip_id, q_before, q_after, defaults):
    """(before_id, after_id) for one announcement - "" for nothing.

    Three places may say, most specific first: the request (`before=` /
    `after=`, "none" to suppress), the clip's own setting, the house default
    (`chime_before` / `chime_after` in the options). A recorded-on-the-spot
    announcement has no clip, so it gets the request or the default.
    """
    meta = _read_meta(clip_id) if clip_id else {}
    out = []
    for key, asked in (("before", q_before), ("after", q_after)):
        if asked is not None:
            v = str(asked).strip()
            out.append("" if v.lower() in ("", "none") else _slug(v))
        elif key in meta:
            out.append(str(meta.get(key) or ""))
        else:
            out.append(str((defaults or {}).get("chime_" + key) or ""))
    return out[0], out[1]


def audio_of(clip_id):
    """The stored bytes of a clip, or None if there is no such clip."""
    p = path_for(clip_id) if clip_id else None
    if not p:
        return None
    with open(p, "rb") as fh:
        return fh.read()


GAP_SECONDS = 0.3
PCM_BYTES_PER_SECOND = 48000 * 2 * 3      # what the sender plays: S24BE stereo


def join_pcm(parts, gap=GAP_SECONDS):
    """Decoded parts, one after another with a short silence between - the
    doorbell, a breath, then the words. Empty parts are skipped."""
    parts = [p for p in parts if p]
    silence = b"\0" * (int(gap * PCM_BYTES_PER_SECOND) // 6 * 6)
    return silence.join(parts)


def save_audio(name, blob, source="recorded", user_facing=True):
    """Keep a recording (or any uploaded audio) as a clip."""
    if not blob:
        raise ClipError("No audio")
    if len(blob) > MAX_BYTES:
        raise ClipError("That is more audio than a page")
    clip_id = _slug(name)
    if not clip_id:
        raise ClipError("Give the clip a name")
    ensure_dir()
    # Written beside the target and moved, so a half-written file is never
    # visible to a driver that is listing clips at the same moment.
    tmp = os.path.join(CLIPS_DIR, "." + clip_id + ".part")
    with open(tmp, "wb") as fh:
        fh.write(blob)
    os.replace(tmp, os.path.join(CLIPS_DIR, clip_id + ".wav"))
    _write_meta(clip_id, name=name, source=source, user_facing=bool(user_facing))
    return clip_id


def delete_clip(clip_id):
    clip_id = _slug(clip_id)
    p = path_for(clip_id)
    if not p:
        raise ClipError("No such clip")
    os.unlink(p)
    try:
        os.unlink(_meta_path(clip_id))
    except OSError:
        pass
    return clip_id


# -- speech ----------------------------------------------------------------
#
# Fish Audio first when a key is set: it is the voice the shipped clips were
# made in (the "Jarvis" voice), so a clip regenerated here sounds like its
# neighbours, and it is the only engine here with a choice of voices and a
# speed that does not warp the pitch. Then Home Assistant, because the house
# has already chosen a voice there; then espeak-ng, because it is offline,
# tiny, and cannot be unavailable.

FISH_TTS_URL = "https://api.fish.audio/v1/tts"
FISH_ASR_URL = "https://api.fish.audio/v1/asr"
# The voice every shipped clip was spoken in. Public on fish.audio.
DEFAULT_VOICE = "049975dde0a14889ad219f24a95e3a4f"
# The alternatives on the editor. Chosen 2026-09-23 from fish.audio's public
# English voices sorted by use: the seven "Fish Official" voices in the top
# 400, plus the most-used calm British narrator. The top of that list by raw
# count is game announcers, meme voices and clones of real people - none of
# which belongs on a client's ceiling speakers - so they were passed over.
VOICES = [
    {"id": DEFAULT_VOICE, "name": "Jarvis (default)", "about": "clear, authoritative male"},
    {"id": "933563129e564b19a115bedd57b7406a", "name": "Sarah", "about": "young female, conversational"},
    {"id": "bf322df2096a46f18c579d0baa36f41d", "name": "Adrian", "about": "male narrator, deep and steady"},
    {"id": "b347db033a6549378b48d00acb0d06cd", "name": "Selene", "about": "female, soft and calm"},
    {"id": "536d3a5e000945adb7038665781a4aca", "name": "Ethan", "about": "male, clear explainer"},
    {"id": "9a9cf47702da476aa4629e2506d4a857", "name": "Hannah", "about": "female, professional"},
    {"id": "79d0bd3e4e5444b18f7b6d89b5927bf1", "name": "Jordan", "about": "older male, confident"},
    {"id": "e3cd384158934cc9a01029cd7d278634", "name": "Laura", "about": "female narrator, warm"},
    {"id": "beb44e5fac1e4b33a15dfcdcc2a9421d", "name": "Historian", "about": "British male, calm"},
]
SPEED_MIN, SPEED_MAX = 0.5, 2.0
DEFAULT_FISH_MODEL = "s2.1-pro"
FISH_FALLBACK_MODEL = "s1"

OPTIONS_FILES = (os.environ.get("SETTINGS_FILE", "/data/settings.json"),
                 os.environ.get("OPTIONS_FILE", "/data/options.json"))
# The house's private fleet file - the one the image seeder writes and
# `bav_house` reads the Cloudflare token from. It lives in Home Assistant's
# own config folder (`homeassistant_config` is mapped at /homeassistant), so a
# key in it is only as exposed as the box, and never in the public add-on
# repository, which anyone can read. /config is the older mapping's name.
FLEET_FILES = tuple(p for p in (os.environ.get("FLEET_FILE"),
                                "/homeassistant/.bav_fleet.json",
                                "/config/.bav_fleet.json") if p)
FISH_WALLET_URL = "https://api.fish.audio/wallet/self/api-credit"


def _options():
    for path in OPTIONS_FILES:
        try:
            with open(path) as fh:
                return json.load(fh)
        except Exception:
            continue
    return {}


def _fleet():
    """(the fleet file's contents, its path) - the first that exists."""
    for path in FLEET_FILES:
        try:
            with open(path) as fh:
                data = json.load(fh)
            return (data if isinstance(data, dict) else {}), path
        except FileNotFoundError:
            continue
        except Exception:
            return {}, path
    return {}, None


def fish_key_source():
    """Where the key comes from: "options" (typed into this add-on), "fleet"
    (the house's private file), "env", or None."""
    if str(_options().get("fish_api_key") or "").strip():
        return "options"
    if str(_fleet()[0].get("fish_api_key") or "").strip():
        return "fleet"
    if os.environ.get("FISH_API_KEY"):
        return "env"
    return None


def fish_settings():
    """(api key, default voice, model) - read fresh, so a key saved on the
    page works on the next click rather than the next restart.

    The add-on's own option wins, so one house can use a different account;
    otherwise the house's fleet file, which is how every house gets the same
    key without it ever being published."""
    o, fleet = _options(), _fleet()[0]
    key = str(o.get("fish_api_key") or fleet.get("fish_api_key") or os.environ.get("FISH_API_KEY") or "").strip()
    voice = str(o.get("fish_voice") or os.environ.get("FISH_VOICE") or DEFAULT_VOICE).strip()
    model = str(o.get("fish_model") or os.environ.get("FISH_MODEL") or DEFAULT_FISH_MODEL).strip()
    return key, voice, model


def check_fish_key(key):
    """Ask Fish whether a key works before keeping it. Returns the API-credit
    balance as a string (or "" if Fish did not say); raises ClipError if the
    key is refused. A network failure is not the key's fault and passes."""
    import urllib.request
    req = urllib.request.Request(FISH_WALLET_URL, headers={"Authorization": "Bearer " + key})
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            data = json.loads(r.read() or b"{}")
    except Exception as e:
        if getattr(e, "code", None) in (401, 403):
            raise ClipError("Fish Audio refused that key")
        return ""
    credit = data.get("credit") if isinstance(data, dict) else None
    return "" if credit is None else str(credit)


def save_fleet_key(key):
    """Keep a Fish key in the house's fleet file, merged with whatever else
    is there (the Cloudflare token, the PIN), readable by root only."""
    key = (key or "").strip()
    if not re.fullmatch(r"[A-Za-z0-9_\-]{16,128}", key):
        raise ClipError("That does not look like a Fish Audio API key")
    credit = check_fish_key(key)
    data, path = _fleet()
    if path is None:
        path = FLEET_FILES[0]
        if not os.path.isdir(os.path.dirname(path)):
            raise ClipError("Home Assistant's config folder is not mapped into this add-on")
    data["fish_api_key"] = key
    tmp = path + ".part"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as fh:
        json.dump(data, fh, indent=2)
        fh.write("\n")
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)
    return {"path": path, "credit": credit}


# Where a house gets the Fish key without anybody typing it: our Hub, asked at
# every start (so every install and update), gated by `admin` + the device PIN.
# The key is then kept in the fleet file above, so a house whose Hub is
# unreachable at boot keeps speaking with the key it already has; a key
# changed on the Hub reaches every house on its next restart.
KEY_SERVER = os.environ.get("KEY_SERVER", "https://logs.bav.homes/house/fish-key")


def fetch_fleet_key(pin, log=print, url=None):
    """Ask the Hub for the Fish key and keep it in the fleet file. Returns
    what happened, in words, for the log. Never raises."""
    import base64
    import urllib.request
    url = KEY_SERVER if url is None else url
    if not url:
        return "key server disabled"
    if fish_key_source() == "options":
        return "a key is typed into this add-on's options; not asking the Hub"
    if not pin:
        return "no device PIN to ask the Hub with"
    auth = base64.b64encode(("admin:" + pin).encode()).decode()
    req = urllib.request.Request(url, headers={
        "Authorization": "Basic " + auth,
        # Cloudflare's bot rules 403 the default Python-urllib agent, which
        # reads exactly like a wrong PIN.
        "User-Agent": "naxaes67send (Home Assistant add-on)"})
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            key = str(json.loads(r.read()).get("fish_api_key") or "").strip()
    except Exception as e:
        return f"the Hub did not give the key ({getattr(e, 'code', None) or type(e).__name__})"
    if not key:
        return "the Hub answered without a key"
    if _fleet()[0].get("fish_api_key") == key:
        return "key from the Hub unchanged"
    try:
        save_fleet_key(key)
    except (ClipError, OSError) as e:
        return f"key from the Hub not kept: {e}"
    return "key from the Hub saved to the fleet file"


def _speed(speed):
    try:
        v = float(speed)
    except (TypeError, ValueError):
        return 1.0
    if v != v:                      # NaN
        return 1.0
    return round(max(SPEED_MIN, min(SPEED_MAX, v)), 2)


def _fish_error(e):
    code = getattr(e, "code", None)
    if code == 402:
        # The JARVIS gotcha: the developer API bills from its own wallet, not
        # the subscription. A funded subscription still gets this.
        return "Fish Audio is out of API credit (fish.audio/app/developers - separate from the subscription)"
    if code == 401:
        return "Fish Audio refused the API key"
    return "Fish Audio did not answer (%s)" % (code or type(e).__name__)


def _tts_via_fish(text, voice, speed, key, model):
    import urllib.request
    body = json.dumps({"text": text[:MAX_TEXT], "format": "mp3", "reference_id": voice,
                       "normalize": True, "prosody": {"speed": speed, "volume": 0}}).encode()
    last = None
    for m in dict.fromkeys([model, FISH_FALLBACK_MODEL]):
        req = urllib.request.Request(FISH_TTS_URL, data=body, method="POST", headers={
            "Authorization": "Bearer " + key, "Content-Type": "application/json",
            # Fish reads the model from this header, not the body.
            "model": m})
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                audio = r.read()
            if audio:
                return audio
        except Exception as e:
            last = e
            if getattr(e, "code", None) in (401, 402):
                break               # the other model will not do better
    raise ClipError(_fish_error(last) if last else "Fish Audio returned no audio")


def _tts_via_home_assistant(text):
    token = os.environ.get("SUPERVISOR_TOKEN")
    if not token:
        return None
    import urllib.request
    body = json.dumps({"message": text[:MAX_TEXT],
                       "platform": os.environ.get("TTS_PLATFORM", "tts.google_en_com")}).encode()
    req = urllib.request.Request(
        "http://supervisor/core/api/tts_get_url", data=body,
        headers={"Authorization": "Bearer " + token,
                 "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            url = json.loads(r.read()).get("url")
        if not url:
            return None
        with urllib.request.urlopen(url, timeout=30) as r:
            return r.read()
    except Exception:
        return None


def _tts_via_espeak(text, speed=1.0):
    try:
        out = subprocess.run(
            ["espeak-ng", "-s", str(int(150 * speed)), "-w", "/dev/stdout", text[:MAX_TEXT]],
            capture_output=True, timeout=30)
        return out.stdout or None
    except Exception:
        return None


# The last few things spoken, so Regenerate saves exactly the take that was
# just previewed. Fish does not say a sentence the same way twice; a person who
# liked what they heard must not get a different reading saved.
_recent = {}
_RECENT_MAX = 6


def synthesize(text, voice=None, speed=1.0):
    """Speak `text`. Returns (audio bytes, a word about who spoke it)."""
    text = (text or "").strip()
    if not text:
        raise ClipError("Nothing to say")
    if len(text) > MAX_TEXT:
        raise ClipError("That is more than a page's worth of words")
    key, default_voice, model = fish_settings()
    speed = _speed(speed)
    voice = (voice or "").strip() or default_voice
    if key:
        if not re.fullmatch(r"[0-9a-f]{32}", voice):
            raise ClipError("That is not a Fish Audio voice id")
        k = (text, voice, speed)
        if k in _recent:
            return _recent[k], "fish"
        audio = _tts_via_fish(text, voice, speed, key, model)
        _recent[k] = audio
        while len(_recent) > _RECENT_MAX:
            _recent.pop(next(iter(_recent)))
        return audio, "fish"
    audio = _tts_via_home_assistant(text)
    if audio:
        return audio, "home-assistant"
    audio = _tts_via_espeak(text, speed)
    if audio:
        return audio, "espeak"
    raise ClipError("No speech engine answered - add a Fish Audio key, or install espeak-ng")


def speak_to_clip(name, text, user_facing=True, voice=None, speed=1.0):
    """Synthesise `text` and keep it as a clip. Returns the clip id."""
    audio, engine = synthesize(text, voice, speed)
    clip_id = save_audio(name or text, audio, source="spoken", user_facing=user_facing)
    meta = _read_meta(clip_id)
    meta.update(text=text.strip(), speed=_speed(speed))
    if engine == "fish":
        meta["voice"] = (voice or "").strip() or fish_settings()[1]
    _write_meta(clip_id, **meta)
    return clip_id


def regenerate_clip(clip_id, text, voice=None, speed=1.0):
    """Speak new words into an existing clip, keeping everything else about it
    - its id (so Crestron programming that plays it still finds it), its name,
    whether it is on the tile, and the sounds around it."""
    clip_id = _slug(clip_id)
    p = path_for(clip_id)
    if not p:
        raise ClipError("No such clip")
    meta = _read_meta(clip_id)
    if meta.get("sound"):
        raise ClipError("That is a sound, not words")
    audio, engine = synthesize(text, voice, speed)
    tmp = os.path.join(CLIPS_DIR, "." + clip_id + ".part")
    with open(tmp, "wb") as fh:
        fh.write(audio)
    os.replace(tmp, p)
    meta.update(text=text.strip(), speed=_speed(speed), source="spoken", text_source="typed")
    if engine == "fish":
        meta["voice"] = (voice or "").strip() or fish_settings()[1]
    else:
        meta.pop("voice", None)
    _write_meta(clip_id, **meta)
    return clip_id


def _multipart(fields, files):
    boundary = "----naxclip" + os.urandom(8).hex()
    out = []
    for k, v in fields.items():
        out.append(("--%s\r\nContent-Disposition: form-data; name=\"%s\"\r\n\r\n%s\r\n"
                    % (boundary, k, v)).encode())
    for k, (fname, ctype, data) in files.items():
        out.append(("--%s\r\nContent-Disposition: form-data; name=\"%s\"; filename=\"%s\"\r\n"
                    "Content-Type: %s\r\n\r\n" % (boundary, k, fname, ctype)).encode() + data + b"\r\n")
    out.append(("--%s--\r\n" % boundary).encode())
    return b"".join(out), "multipart/form-data; boundary=" + boundary


def _transcribe(path, key):
    import urllib.request
    with open(path, "rb") as fh:
        audio = fh.read()
    body, ctype = _multipart({"language": "en", "ignore_timestamps": "true"},
                             {"audio": ("clip", content_type(path), audio)})
    req = urllib.request.Request(FISH_ASR_URL, data=body, method="POST", headers={
        "Authorization": "Bearer " + key, "Content-Type": ctype})
    with urllib.request.urlopen(req, timeout=60) as r:
        return (json.loads(r.read()).get("text") or "").strip()


def clip_text(clip_id, log=print):
    """What a clip says, for the editor: {text, from}.

    Stored text if there is any. The 36 shipped clips were recorded before the
    words were kept, so the first time one is opened it is transcribed (Fish
    speech-to-text, same key) and the result kept beside it - a clip is heard
    once and never paid for again. Without a key, or if that fails, the clip's
    name, which for most of them is close to the words anyway.
    """
    clip_id = _slug(clip_id)
    p = path_for(clip_id)
    if not p:
        raise ClipError("No such clip")
    meta = _read_meta(clip_id)
    if meta.get("text"):
        return {"text": meta["text"], "from": meta.get("text_source") or "typed",
                "voice": meta.get("voice"), "speed": meta.get("speed") or 1.0}
    name = meta.get("name") or clip_id.replace("-", " ").title()
    key = fish_settings()[0]
    if key:
        try:
            text = _transcribe(p, key)
        except Exception as e:
            log(f"[clips] {clip_id}: could not transcribe ({_fish_error(e)})")
            text = ""
        if text:
            meta.update(text=text, text_source="transcribed")
            _write_meta(clip_id, **meta)
            log(f"[clips] {clip_id}: transcribed and kept")
            return {"text": text, "from": "transcribed", "voice": meta.get("voice"), "speed": meta.get("speed") or 1.0}
    return {"text": name, "from": "name", "voice": meta.get("voice"), "speed": meta.get("speed") or 1.0}
