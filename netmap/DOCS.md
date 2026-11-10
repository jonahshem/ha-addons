# BAV NetMap

Maps the house network on a schedule and watches the devices that matter. It runs on the
rack's Home Assistant box, so it sees the LAN the way a laptop plugged into the same switch
would. The scanner is the same `netscan.py` as the field tool in `Tools/netmap`.

## What it does

- **Scans.** It maps the whole subnet on start, then every `scan_interval_hours` (default 6).
  The **Scan now** button on the NetMap panel runs one immediately. Each scan finds IP, MAC,
  vendor, names, open ports, and the model and firmware of Crestron devices.
  - It never opens Modbus (502) or CoolMaster (10102) sessions, because the processor polls
    those boxes.
- **Keeps a plan.** Results go to `/share/netmap/plan`: `devices.csv`, `networks.csv` and
  `report.html`. The Samba share shows this folder as `share/netmap/plan`.
  - You can edit the plan columns there; the next scan keeps your edits.
  - The laptop's `unifi_import.py` reads the same files.
  - Put the old integrator's IP sheet in `share/netmap/known/` and its device names are used
    in every scan.
- **Monitors.** Every row with `reserve=Y` is checked every `monitor_interval_seconds`
  (default 60). Devices that ignore ping are checked by TCP on a web, SSH or Crestron port
  instead.
  - A device that misses `down_after_checks` checks in a row (default 3) is marked down.
  - Addresses in `fast_watch` are checked every 5 s. Use this for devices that reset
    themselves: a reset of a few seconds shows up as a **blip**.
- **Tells Home Assistant:**
  - `binary_sensor.netmap_<name>_<last octet>` (connectivity) for each watched device;
  - `sensor.netmap_offline`, which counts the down devices and lists them;
  - the events `netmap_device_offline`, `netmap_device_online` and `netmap_new_device`, for
    use in automations.
- **Notifies.** When a device has been down for `notify_after_seconds` (default 3 minutes),
  you get a notification, and another when it's back. Shorter outages are only counted as
  blips. New devices also get a notification, except phones on private MACs.
  - Set `notify_service` to, for example, `mobile_app_jonahs_pixel`. If it's blank, the
    notification appears in Home Assistant itself.

## Setup on a rack Pi

1. **Hardware:** use the official 5.1 V 3 A supply and boot from SSD rather than SD. Put it
   on the rack UPS. An under-powered Pi 4 throttles and corrupts its storage.
2. **Network:** plug it into the LAN you want mapped. For the old network before cutover,
   use the old switch.
3. **Install:** add this repository to the Add-on Store and install **BAV NetMap**. Set
   `site`, and start it. The first scan takes about a minute.
4. **Other subnets:** add them, such as a camera VLAN or a routed subnet, under `subnets`.

`/share/netmap/` holds the plan and the known sheets. The add-on's own data (`/data`) holds
its state and a 24-hour event log.
