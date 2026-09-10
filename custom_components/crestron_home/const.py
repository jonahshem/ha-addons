DOMAIN = "crestron_home"
DEFAULT_POLLING_INTERVAL = 5
PLATFORMS = [
    "light",
    "cover",
    "climate",
    "lock",
    "scene",
    "alarm_control_panel",
    "binary_sensor",  # For /sensors (Occupancy, Motion, Door/Window)
    "button",         # For genericIO/relay scenes (Gates, etc.)
    "media_player",   # For /mediarooms (read-only status)
    "switch",         # For PC-350 PDU outlets (verified WebSocket-based)
]
