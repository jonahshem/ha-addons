"""The add-on: one AES67 stream, an API that speaks into it, and a look
around the network for the amplifiers it should be speaking to."""
import os
import threading
import time

import api
import clips
import discover
import sender

# The shipped announcements, into the house's folder, once each. Before the
# API is up so the driver's first poll already sees them.
try:
    clips.seed_bundled(log=api.log)
except Exception as e:
    api.log(f"[clips] seeding the bundled announcements failed: {e!r}")

pipe = sender.build()
threading.Thread(target=api.serve, daemon=True).start()
# Finish any restore the last run never got to, then point every receive slot
# at the stream - in the background, so neither the stream nor the API waits,
# and so the first page after a restart pays for neither (0.15.0).
threading.Thread(target=api.startup, daemon=True, name="startup").start()
# The Fish key, from our Hub - at every start, so an install or an update is
# all a house needs. Off the main thread: a slow Hub must not delay the stream.
threading.Thread(
    target=lambda: api.log("[clips] " + clips.fetch_fleet_key(
        os.environ.get("AMP_PASSWORD") or os.environ.get("CRPC_PIN") or "", log=api.log)),
    daemon=True).start()
if (os.environ.get("AUTODETECT") or "true").lower() in ("1", "true", "yes", "on"):
    # At start and every six hours: a house whose options say `amps: []` and
    # whose rack holds six DM-NAX should not stay silent because nobody typed
    # six addresses. Adds only; never touches an amplifier somebody entered.
    threading.Thread(
        target=discover.loop, daemon=True,
        args=(lambda: sender.local_ip(sender.MCAST), os.environ.get("AMP_USER") or "admin",
              os.environ.get("AMP_PASSWORD") or "", api.amplifiers),
        kwargs={"log": api.log, "subnet": (os.environ.get("SUBNET") or "").strip() or None,
                # Every pass, and so every six hours: warm the receive slots,
                # including on any amplifier this pass just added.
                "after": lambda r: api.prewarm(
                    "found " + ", ".join(r["added"]) if r.get("added") else "six-hourly")},
    ).start()
else:
    # No discovery to hang it on: warm the slots every six hours anyway.
    def _rewarm():
        while True:
            time.sleep(discover.EVERY)
            api.prewarm("six-hourly")
    threading.Thread(target=_rewarm, daemon=True, name="rewarm").start()
sender.run_forever(pipe)
