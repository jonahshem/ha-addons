ARG BUILD_FROM
FROM ${BUILD_FROM}
RUN apt-get update && apt-get install -y --no-install-recommends \
      linuxptp ethtool tcpdump iproute2 iputils-ping ca-certificates \
    && rm -rf /var/lib/apt/lists/*
COPY run.sh /run.sh
RUN chmod a+x /run.sh
CMD ["/run.sh"]
