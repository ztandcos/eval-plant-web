FROM ubuntu:24.04
ENV DEBIAN_FRONTEND=noninteractive
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        python3 curl bash ca-certificates coreutils \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /app
RUN mkdir -p /data
COPY a.json /data/a.json
COPY b.json /data/b.json
