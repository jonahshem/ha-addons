"""The zone list a person sees, shaped like the house rather than the rack.

Two things the raw amplifier listing gets wrong, both seen on the driver's
Announcements page at 110 Roosevelt (2026-09-14, six DM-NAX, 39 outputs):

* **A bussed pair is one room.** The Snug is Zone5 + Zone6 on one amplifier,
  `IsBussed: true, BusId: 1` on both - a stereo zone and a bridged-mono
  partner. Listed raw it appears twice. Measured: routing Zone5 alone switches
  Zone6 with it, raises and restores both. So the pair is one zone, named by
  its lowest-numbered member, and the partner is not listed at all.
* **The house's list is Crestron Home's, not the amplifiers'.** Home had 43
  media rooms; the amplifiers 36 distinct zones. The list people expect is
  Home's, in Home's order, with each room's speakers found by name - and a
  room whose speakers this box cannot find still listed, marked, rather than
  silently absent. A zone Home has no room for (an unnamed spare output) goes
  last, marked the other way.

Only `compose` is used outside; the rest is its parts.
"""
import re

PLAIN = str.maketrans({"’": "'", "‘": "'", "“": '"', "”": '"', " ": " "})
NO_SPEAKERS = "no speakers found"
NOT_A_ROOM = "not a room in Crestron Home"


def norm(name):
    """'Steve’s Closet' and "steve's  closet" are the same room."""
    return re.sub(r"\s+", " ", str(name or "").translate(PLAIN)).strip().casefold()


def collapse_buses(zones):
    """{id: info} with every bus partner (`bus_of` set) folded into its primary.

    `zones()` in naxctl marks the partners; this drops them, and the primary
    keeps `members` so anything that cares can see what it drives.
    """
    return {k: v for k, v in zones.items() if not (v or {}).get("bus_of")}


def compose(zones, rooms):
    """Shape `zones` ({id: info}) around `rooms` ([{id, name}] from Crestron
    Home, in its order); `rooms` None means no processor - the amplifier
    order stands, buses still collapsed."""
    zones = collapse_buses(zones)
    if rooms is None:
        return zones
    by_name = {}
    for zid, info in zones.items():
        by_name.setdefault(norm((info or {}).get("name")), []).append(zid)
    out = {}
    for room in rooms:
        rname = (room or {}).get("name") or ""
        rid = (room or {}).get("id")
        for zid in by_name.pop(norm(rname), []):
            out[zid] = dict(zones[zid], room=rname, room_id=rid)
        else:
            if norm(rname) and f"room:{rid}" not in out and not any(
                    v.get("room_id") == rid for v in out.values()):
                out[f"room:{rid}"] = {
                    "name": f"{rname} ({NO_SPEAKERS})", "room": rname, "room_id": rid,
                    "host": "", "zone": "", "source": "", "signal": False, "stray": False,
                    "volume": None, "unavailable": NO_SPEAKERS}
    for ids in by_name.values():
        for zid in ids:
            info = dict(zones[zid], home=False)
            info["name"] = f"{info.get('name') or zid} ({NOT_A_ROOM})"
            out[zid] = info
    return out


def targets_only(refs):
    """Split announce targets into the ones an amplifier can take and the
    room-only placeholders that nothing can."""
    real = [r for r in refs if not str(r).startswith("room:")]
    return real, [r for r in refs if str(r).startswith("room:")]
