#!/usr/bin/with-contenv bash
# with-contenv, not plain bash: the Supervisor hands SUPERVISOR_TOKEN to the
# container through s6's environment directory, and a script started outside
# it never sees the variable - discovery then finds the house but cannot write
# what it found into the options.
export CONF=/data/options.json
echo "===== Rava bridge $(date) ====="
# Which Supervisor-provided variables reached us - names only, never values.
echo "env: $(env | grep -i -E '^(SUPERVISOR|HASSIO|HOMEASSISTANT)[A-Z_]*=' | sed 's/=.*//' | tr '\n' ' ')"
python3 -c "import json;c=json.load(open('$CONF'));print('panels:',[p.get('name') for p in c.get('panels') or []]);print('doors:',[d.get('name') for d in c.get('doors') or []]);print('mcast:',c.get('mcast'),c.get('mcast_port'))"
exec python3 -u /main.py
