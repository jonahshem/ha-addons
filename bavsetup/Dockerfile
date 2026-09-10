ARG BUILD_FROM
FROM ${BUILD_FROM}
# Standard library only. ca-certificates is what the GitHub fetch needs.
RUN apt-get update && apt-get install -y --no-install-recommends \
      python3 ca-certificates \
    && rm -rf /var/lib/apt/lists/*
COPY run.sh /run.sh
COPY main.py /main.py
RUN chmod a+x /run.sh
CMD ["/run.sh"]
