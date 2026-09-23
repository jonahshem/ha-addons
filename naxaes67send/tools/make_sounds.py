"""Synthesise the shipped doorbell sounds into announcements/.

Run:  python NaxAes67Sender/tools/make_sounds.py

Pure standard library, so it runs anywhere without a sound library installed.
Each sound is a few struck notes: a sum of partials, each with its own
exponential decay, the way a real bar or tube rings - the high partials die
first, which is what makes a bell sound struck rather than switched on.

Written to match `ding-dong.wav` and `chime.wav`: 48 kHz, 16-bit, mono, peak
-1.9 dBFS. The sender converts to its own wire format at play time, so these
only need to be clean, not in any particular format.

Not copied into the image; the WAVs it writes are. Re-run it only to change a
sound, then commit the WAVs.
"""
import array
import json
import math
import os
import random
import wave

RATE = 48000
PEAK_DBFS = -1.9
RMS_CEILING_DBFS = -14.0     # ding-dong measures -15.0, chime -15.5
OUT = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "announcements")

# Equal-tempered pitches used below.
N = {"G4": 392.00, "A4": 440.00, "B4": 493.88, "C5": 523.25, "D5": 587.33, "E5": 659.25,
     "F5": 698.46, "G5": 783.99, "A5": 880.00, "B5": 987.77, "C6": 1046.50}

# (ratio to the fundamental, level, decay time constant in seconds)
TUBE = [(1.0, 1.0, 1.6), (2.76, 0.45, 0.9), (5.40, 0.22, 0.45), (8.93, 0.10, 0.22)]
BAR = [(1.0, 1.0, 1.1), (3.0, 0.25, 0.5), (4.1, 0.12, 0.3)]          # a chime bar
MARIMBA = [(1.0, 1.0, 0.45), (3.93, 0.35, 0.12), (9.2, 0.10, 0.05)]
RINGER = [(1.0, 1.0, 0.35), (2.32, 0.6, 0.25), (4.25, 0.35, 0.15), (6.8, 0.2, 0.08)]


def silence(seconds):
    return [0.0] * int(seconds * RATE)


def strike(buf, at, freq, partials, length, level=1.0, attack=0.004):
    """Add one struck note into `buf` starting at `at` seconds."""
    start = int(at * RATE)
    n = int(length * RATE)
    if len(buf) < start + n:
        buf.extend([0.0] * (start + n - len(buf)))
    a = int(attack * RATE) or 1
    # A note still ringing when its window ends must be faded, not cut: the
    # cut is a click, visible as a vertical line on a spectrogram.
    rel = int(min(0.4, length / 3) * RATE)
    for ratio, amp, tau in partials:
        f = freq * ratio
        if f >= RATE / 2:
            continue
        w = 2 * math.pi * f / RATE
        k = math.exp(-1.0 / (tau * RATE))
        env = amp * level
        for i in range(n):
            ramp = i / a if i < a else 1.0
            if i > n - rel:
                ramp *= (n - i) / rel
            buf[start + i] += env * ramp * math.sin(w * i)
            env *= k


def tone(buf, at, freq, length, level=1.0, harmonics=((1, 1.0), (3, 0.18), (5, 0.06))):
    """A switched electronic tone: soft edges, no decay while it is held."""
    start = int(at * RATE)
    n = int(length * RATE)
    if len(buf) < start + n:
        buf.extend([0.0] * (start + n - len(buf)))
    edge = int(0.012 * RATE)
    for i in range(n):
        e = min(1.0, i / edge, (n - i) / edge) * level
        buf[start + i] += e * sum(amp * math.sin(2 * math.pi * freq * h * i / RATE) for h, amp in harmonics)


def westminster():
    """The Westminster Quarters as a doorbell: E C D G, then G D E C, the last
    note left to ring. The tune Big Ben's quarter bells play."""
    buf = silence(0.05)
    t = 0.05
    for i, note in enumerate(["E5", "C5", "D5", "G4", "G4", "D5", "E5", "C5"]):
        last = i == 7
        strike(buf, t, N[note], TUBE, 3.2 if last else 2.2, level=1.0 if note != "G4" else 1.15)
        t += 0.62 if i != 3 else 1.05       # a breath between the two phrases
    return buf


def three_chime():
    """Three rising bars, C E G - the bright 'ding-ding-ding'."""
    buf = silence(0.05)
    for i, note in enumerate(["C5", "E5", "G5"]):
        strike(buf, 0.05 + i * 0.32, N[note], BAR, 2.0 if i == 2 else 1.4)
    return buf


def tubular():
    """Four deep tubes falling C6 A5 F5 C5, slow - a grander 'someone is here'."""
    buf = silence(0.05)
    for i, note in enumerate(["C6", "A5", "F5", "C5"]):
        strike(buf, 0.05 + i * 0.55, N[note], TUBE, 3.0 if i == 3 else 2.0)
    return buf


def marimba():
    """A warm wooden arpeggio up and back down, G B D G D."""
    buf = silence(0.05)
    for i, note in enumerate(["G4", "B4", "D5", "G5", "D5"]):
        strike(buf, 0.05 + i * 0.16, N[note], MARIMBA, 1.2, level=1.1 if i == 3 else 0.9)
    return buf


def old_bell():
    """The mechanical ringer: a clapper hammering a small bell about 20 times a
    second, in two bursts - 'brrring, brrring'."""
    rnd = random.Random(7)                   # the same file every run
    buf = silence(0.05)
    for burst in (0.05, 1.05):
        t = burst
        while t < burst + 0.7:
            strike(buf, t, 1480.0, RINGER, 0.6, level=0.55 + 0.1 * rnd.random(), attack=0.001)
            t += 0.048 + 0.004 * rnd.random()
    buf.extend(silence(0.4))
    return buf


def electronic():
    """A modern two-tone electronic bell, played twice: bing-bong, bing-bong."""
    buf = silence(0.05)
    for rep in (0.05, 0.95):
        tone(buf, rep, N["B5"], 0.28)
        tone(buf, rep + 0.32, N["G5"], 0.42)
    buf.extend(silence(0.3))
    return buf


SOUNDS = [
    ("westminster", "Westminster Chimes", westminster),
    ("three-chime", "Three-Note Chime", three_chime),
    ("tubular-bells", "Tubular Bells", tubular),
    ("marimba", "Marimba", marimba),
    ("old-bell", "Old-Fashioned Bell", old_bell),
    ("electronic", "Electronic Bing-Bong", electronic),
]


def write(clip_id, name, buf):
    # A short fade on the very end so nothing clicks, then normalise.
    fade = int(0.08 * RATE)
    for i in range(1, fade + 1):
        buf[-i] *= i / fade
    # Peak to -1.9 dBFS, unless that makes it louder than the others: a held
    # electronic tone carries far more energy than a bell at the same peak,
    # and a doorbell that is suddenly 10 dB louder than the chime is a fault.
    peak = max(abs(x) for x in buf) or 1.0
    rms = math.sqrt(sum(x * x for x in buf) / len(buf)) or 1.0
    gain = min((10 ** (PEAK_DBFS / 20)) * 32767 / peak, (10 ** (RMS_CEILING_DBFS / 20)) * 32767 / rms)
    pcm = array.array("h", (int(round(x * gain)) for x in buf))
    with wave.open(os.path.join(OUT, clip_id + ".wav"), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(RATE)
        w.writeframes(pcm.tobytes())
    with open(os.path.join(OUT, clip_id + ".json"), "w") as fh:
        json.dump({"name": name, "source": "bundled", "user_facing": False, "sound": True}, fh)
    rms = math.sqrt(sum(x * x for x in pcm) / len(pcm)) / 32768
    print(f"{clip_id:14} {len(pcm) / RATE:4.1f}s  rms {20 * math.log10(rms):5.1f} dBFS")


if __name__ == "__main__":
    for clip_id, name, fn in SOUNDS:
        write(clip_id, name, fn())
