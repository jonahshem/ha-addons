ARG BUILD_FROM
FROM ${BUILD_FROM}
# Standard library only: python3 is the whole dependency.
# python3-paramiko only for discovery's SIPINFO over SSH; the bridge itself needs nothing.
RUN apt-get update && apt-get install -y --no-install-recommends python3 python3-paramiko ffmpeg iproute2 tcpdump ca-certificates \
    && rm -rf /var/lib/apt/lists/*
COPY run.sh /run.sh
COPY main.py /main.py
COPY bridge.py /bridge.py
COPY api.py /api.py
COPY discover.py /discover.py
COPY pages.py /pages.py
COPY pageaudio.py /pageaudio.py
COPY protect.py /protect.py
COPY protectdoor.py /protectdoor.py
COPY crpc.py /crpc.py
COPY sip.py /sip.py
COPY rtp.py /rtp.py
RUN chmod a+x /run.sh
CMD ["/run.sh"]
