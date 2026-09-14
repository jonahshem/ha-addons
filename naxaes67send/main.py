"""The add-on: one AES67 stream, an API that speaks into it, and a look
around the network for the amplifiers it should be speaking to."""
import os
import threading

import api
import discover
import sender

pipe = sender.build()
threading.Thread(target=api.serve, daemon=True).start()
if (os.environ.get("AUTODETECT") or "true").lower() in ("1", "true", "yes", "on"):
    # At start and every six hours: a house whose options say `amps: []` and
    # whose rack holds six DM-NAX should not stay silent because nobody typed
    # six addresses. Adds only; never touches an amplifier somebody entered.
    threading.Thread(
        target=discover.loop, daemon=True,
        args=(lambda: sender.local_ip(sender.MCAST), os.environ.get("AMP_USER") or "admin",
              os.environ.get("AMP_PASSWORD") or "", api.amplifiers),
        kwargs={"log": api.log, "subnet": (os.environ.get("SUBNET") or "").strip() or None},
    ).start()
sender.run_forever(pipe)
