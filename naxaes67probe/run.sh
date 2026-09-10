#!/bin/bash
IFACE=$(ip route get 192.168.0.51 2>/dev/null | grep -o 'dev [^ ]*' | awk '{print $2}'); [ -z "$IFACE" ] && IFACE=end0
echo "===== RTP CHECK $(date) ====="
timeout 6 tcpdump -i "$IFACE" -n 'udp and dst 239.69.4.4 and dst port 5004' 2>&1 | tail -3
echo "===== RTP DONE ====="; tail -f /dev/null
