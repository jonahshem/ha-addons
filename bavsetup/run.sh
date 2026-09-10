#!/usr/bin/with-contenv bash
# with-contenv, not plain bash: SUPERVISOR_TOKEN reaches the container through
# s6's environment directory, and a script started outside it never sees the
# variable - the install then works and the restart afterwards does not.
export CONF=/data/options.json
echo "===== BAV House Setup $(date) ====="
echo "env: $(env | grep -i -E '^(SUPERVISOR|HASSIO)[A-Z_]*=' | sed 's/=.*//' | tr '\n' ' ')"
exec python3 -u /main.py
