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
                new = {k: meta[k] for k in ("before", "after", "sound") if k in meta and k not in have}
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
        for key in ("sound", "before", "after"):
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
# Two backends, tried in order. Home Assistant first because the house has
# already chosen a voice there and it is almost always a better one than
# anything this container would ship; espeak-ng second because it is offline,
# tiny, and cannot be unavailable.

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


def _tts_via_espeak(text):
    try:
        out = subprocess.run(
            ["espeak-ng", "-s", "150", "-w", "/dev/stdout", text[:MAX_TEXT]],
            capture_output=True, timeout=30)
        return out.stdout or None
    except Exception:
        return None


def speak_to_clip(name, text, user_facing=True):
    """Synthesise `text` and keep it as a clip. Returns the clip id."""
    text = (text or "").strip()
    if not text:
        raise ClipError("Nothing to say")
    if len(text) > MAX_TEXT:
        raise ClipError("That is more than a page's worth of words")
    audio = _tts_via_home_assistant(text) or _tts_via_espeak(text)
    if not audio:
        raise ClipError("No speech engine answered - is espeak-ng installed?")
    return save_audio(name or text, audio, source="spoken", user_facing=user_facing)
