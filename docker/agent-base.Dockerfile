FROM node:22-bookworm

ENV DEBIAN_FRONTEND=noninteractive \
    npm_config_fetch_retries=5 \
    npm_config_fetch_retry_maxtimeout=120000

RUN set -eux; \
    for attempt in 1 2 3; do \
      apt-get update && apt-get install -y --no-install-recommends \
        python3 curl bash ca-certificates coreutils procps ripgrep && break; \
      test "$attempt" = 3 && exit 1; sleep "$((attempt * 10))"; \
    done; \
    rm -rf /var/lib/apt/lists/*

RUN set -eux; \
    for attempt in 1 2 3; do \
      npm install -g @openai/codex @anthropic-ai/claude-code @zed-industries/claude-code-acp@0.16.2 && break; \
      test "$attempt" = 3 && exit 1; sleep "$((attempt * 10))"; \
    done; \
    codex --version; claude --version; claude-code-acp --version

WORKDIR /app
