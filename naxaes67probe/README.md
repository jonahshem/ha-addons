# NAX AES67 Probe

A read-only feasibility probe for AES67 into a DM NAX: captures six seconds of RTP
on the AES67 multicast group with tcpdump and prints the tail, so a house can show
whether the sender's stream is actually arriving on the LAN before anything is routed.

Diagnostic, `boot: manual`. It first lived only on the 14 Malke Pi; brought into the
repo 2026-09-07 so the add-on repository can carry it.
