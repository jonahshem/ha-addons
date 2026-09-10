#!/bin/bash
# The amplifier's own subnet first, then the multicast group the stream would
# use. `end0` is only a Raspberry Pi's name for its built-in NIC, so it is no
# use as a fallback on the Intel boxes this now also builds for.
route_dev() { ip route get "$1" 2>/dev/null | grep -o 'dev [^ ]*' | awk '{print $2}'; }
IFACE=$(route_dev 192.168.0.51); [ -z "$IFACE" ] && IFACE=$(route_dev 239.69.4.4)
echo "===== RTP CHECK $(date) ====="
timeout 6 tcpdump -i "$IFACE" -n 'udp and dst 239.69.4.4 and dst port 5004' 2>&1 | tail -3
echo "===== RTP DONE ====="; tail -f /dev/null
