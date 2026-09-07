ARG BUILD_FROM
FROM ${BUILD_FROM}
RUN apt-get update && apt-get install -y --no-install-recommends \
      gstreamer1.0-tools gstreamer1.0-plugins-base gstreamer1.0-plugins-good \
      gstreamer1.0-plugins-bad gstreamer1.0-libav \
      python3 python3-gi python3-gst-1.0 python3-websocket \
      gir1.2-gstreamer-1.0 gir1.2-gst-plugins-base-1.0 \
      linuxptp espeak-ng iproute2 iputils-ping ethtool tcpdump ca-certificates \
    && rm -rf /var/lib/apt/lists/*
COPY run.sh /run.sh
COPY main.py /main.py
COPY sender.py /sender.py
COPY sap.py /sap.py
COPY naxctl.py /naxctl.py
COPY api.py /api.py
COPY clips.py /clips.py
RUN chmod a+x /run.sh
CMD ["/run.sh"]
