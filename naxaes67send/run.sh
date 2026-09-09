#!/usr/bin/with-contenv bash
# Add-on options arrive as a JSON file; turn the ones we need into env.
CONF=/data/options.json
get() { python3 -c "import json,sys;print(json.load(open('$CONF')).get('$1','') or '')" 2>/dev/null; }
export IFACE=${IFACE:-$(get iface)}; export IFACE=${IFACE:-end0}
export MCAST=$(get mcast);     export MCAST=${MCAST:-239.69.4.4}
export PORT=$(get port);       export PORT=${PORT:-5004}
export SESSION=$(get session); export SESSION=${SESSION:-HA Announce 1}
export NAX_HOST=$(get nax_host)
export NAX_USER=$(get nax_user); export NAX_USER=${NAX_USER:-admin}
export NAX_PASS=$(get nax_password)
# The amplifiers, as JSON rather than as N sets of numbered variables. There
# is no sensible shell shape for a list of records, and `api.py` wants it as
# a list anyway.
export AMPS_JSON=$(python3 -c "import json;print(json.dumps(json.load(open('$CONF')).get('amps') or []))" 2>/dev/null || echo '[]')
export API_TOKEN=$(get api_token)
export API_PORT=$(get api_port); export API_PORT=${API_PORT:-8099}
# Padding around a clip. Empty is fine - api.py carries the defaults.
export LEAD_SECONDS=$(get lead_seconds)
export TAIL_SECONDS=$(get tail_seconds)
export ANNOUNCE_VOLUME=$(get announce_volume)
export CRPC_HOST=$(get crpc_host)
export CRPC_PIN=$(get crpc_pin)
echo "===== NAX AES67 sender $(date) ====="
echo "iface=$IFACE mcast=$MCAST:$PORT session='$SESSION' api=$API_PORT"
echo "lead=${LEAD_SECONDS:-1.5}s tail=${TAIL_SECONDS:-3.0}s announce_volume=${ANNOUNCE_VOLUME:-600}"
# Addresses only. The passwords are in the same file and must not be.
python3 -c "import json,os;print('amps:', [a.get('host') for a in json.loads(os.environ['AMPS_JSON'])] or [os.environ.get('NAX_HOST') or '(none)'])"
exec python3 -u /main.py
