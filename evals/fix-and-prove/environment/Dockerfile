FROM ubuntu:24.04
ENV DEBIAN_FRONTEND=noninteractive
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        python3 curl bash ca-certificates coreutils \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY calc.py /app/calc.py
COPY run_tests.py /app/run_tests.py
