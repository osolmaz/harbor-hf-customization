FROM docker.io/library/node:24.18.0-bookworm@sha256:4e9cb555d708e0829c9d93e5eeae9dfab0617b832ca436a690680e0fca735ef5 AS build
FROM ghcr.io/astral-sh/uv:0.12.5@sha256:db2d5999728c5837e1bf9ba278ee6b05cef1e95e82a20e27b0c915cb4478b9d7 AS uv
FROM build AS package
RUN corepack enable \
    && corepack prepare pnpm@12.3.4 --activate
RUN git clone --filter=blob:none https://github.com/openclaw/openclaw.git /src/openclaw \
    && cd /src/openclaw \
    && git checkout 651775ba8d2c7b10f37c5372d3528b41ecfbc804 \
    && test "$(git rev-parse HEAD)" = 651775ba8d2c7b10f37c5372d3528b41ecfbc804
WORKDIR /src/openclaw
RUN pnpm install --frozen-lockfile
RUN node scripts/package-openclaw-for-docker.mjs \
      --allow-unreleased-changelog \
      --output-dir /payload \
      --output-name openclaw.tgz
RUN cd /payload \
    && printf '%s\n' '{"private":true,"dependencies":{"openclaw":"file:openclaw.tgz"}}' > package.json \
    && npm install --package-lock-only --ignore-scripts --no-audit --no-fund \
    && mkdir -p bin lib/node_modules \
    && cp /usr/local/bin/node bin/node \
    && cp -a /usr/local/lib/node_modules/npm lib/node_modules/npm \
    && cp /usr/local/LICENSE node-LICENSE \
    && cp /src/openclaw/LICENSE openclaw-LICENSE \
    && cp /src/openclaw/THIRD_PARTY_NOTICES.md openclaw-THIRD_PARTY_NOTICES.md \
    && printf '%s\n' '{"nodeVersion":"24.18.0","openclawVersion":"2026.9.3","sourceCommit":"651775ba8d2c7b10f37c5372d3528b41ecfbc804"}' > provenance.json
COPY --from=uv /uv /usr/local/bin/uv
COPY runtimes/openclaw-native /runtime
WORKDIR /runtime
RUN cp -a /payload payload \
    && uv build --wheel --python 3.12 \
    && uv run --no-project --python 3.12 --with /runtime/dist/*.whl python tests/payload_smoke.py
FROM scratch AS artifact
COPY --from=package /runtime/dist/ /
