"""The add-on: read the options, start the bridge, serve the API, look for the house."""
import json
import os
import sys
import threading
import time

import api
import bridge

DISCOVER_EVERY = 6 * 3600


def log(msg):
    print(time.strftime("%H:%M:%S"), msg, flush=True)


def load():
    path = os.environ.get("CONF") or (sys.argv[1] if len(sys.argv) > 1 else "/data/options.json")
    with open(path, encoding="utf-8") as f:
        cfg = json.load(f)
    if cfg.get("autodetect", True) and os.path.isdir("/data"):
        import discover
        cfg = discover.load_local(cfg)
    return cfg


def discovery_loop():
    # A first pass shortly after start - the panels are on the LAN already, and a
    # house whose options are empty should ring without anybody typing an IP.
    time.sleep(5)
    while True:
        try:
            api.run_discovery(reason="scheduled")
        except Exception as e:
            log(f"discover: {e!r}")
        time.sleep(DISCOVER_EVERY)


if __name__ == "__main__":
    cfg = load()
    b = bridge.Bridge(cfg, log)
    b.start()
    threading.Thread(target=api.serve, args=(b, cfg, log), daemon=True).start()
    if cfg.get("autodetect", True):
        threading.Thread(target=discovery_loop, daemon=True).start()
    if cfg.get("page_listen", True):
        import pages
        b.pages = pages.PageListener(b, cfg.get("page_group") or "227.1.1.1", cfg.get("page_port") or 1234, log,
                                     relay=cfg.get("page_relay") or {})
        b.pages.start()
    import crpc
    b.crpc = crpc.CrpcManager(cfg, log)
    if b.crpc.enabled:
        b.crpc.start()
    while True:
        time.sleep(3600)
