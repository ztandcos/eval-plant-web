FROM python:3.11-slim

RUN apt-get update \
    && apt-get install -y --no-install-recommends bash git ripgrep \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /opt/evalplant
COPY pyproject.toml README.md ./
COPY evalplant ./evalplant
COPY prompts ./prompts
RUN python3 -m pip install --no-cache-dir . mini-swe-agent==2.4.6 uv==0.11.8

ENV PYTHONDONTWRITEBYTECODE=1
WORKDIR /task
ENTRYPOINT ["evalplant"]
CMD ["--help"]
