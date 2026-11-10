#!/usr/bin/with-contenv bash
# with-contenv: SUPERVISOR_TOKEN (and TZ) only reach a script started inside s6's environment.
echo "===== BAV NetMap $(date) ====="
exec python3 -u /app/monitor.py
