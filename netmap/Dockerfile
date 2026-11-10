ARG BUILD_FROM
FROM ${BUILD_FROM}
# Standard library only. ping for the monitor; tzdata so times are the house's, not UTC.
RUN apt-get update && apt-get install -y --no-install-recommends \
      python3 iputils-ping ca-certificates tzdata \
    && rm -rf /var/lib/apt/lists/*
COPY run.sh /run.sh
COPY netscan.py monitor.py /app/
RUN chmod a+x /run.sh
CMD ["/run.sh"]
