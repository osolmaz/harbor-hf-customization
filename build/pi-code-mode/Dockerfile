FROM docker.io/library/node:24.18.0-bookworm-slim@sha256:6f7b03f7c2c8e2e784dcf9295400527b9b1270fd37b7e9a7285cf83b6951452d AS node
FROM ghcr.io/astral-sh/uv:0.12.5@sha256:db2d5999728c5837e1bf9ba278ee6b05cef1e95e82a20e27b0c915cb4478b9d7 AS uv
FROM docker.io/library/rust:1.96.1-bookworm@sha256:a339861ae23e9abb272cea45dfafde21760d2ce6577a70f8a926153677902663 AS build
COPY --from=node /usr/local /usr/local
COPY --from=uv /uv /usr/local/bin/uv
ENV CARGO_BUILD_JOBS=2
RUN git clone --filter=blob:none https://github.com/osolmaz/pi-code-mode.git /src/pi-code-mode \
    && cd /src/pi-code-mode \
    && git checkout 66bcfd51d54d8a1b7ab2bd9cb756b65874af9dda \
    && test "$(git rev-parse HEAD)" = 66bcfd51d54d8a1b7ab2bd9cb756b65874af9dda \
    && npm ci --ignore-scripts \
    && npm run build
WORKDIR /repo
COPY build/pi-code-mode/package*.json /payload/
RUN cd /payload && npm ci --omit=dev \
    && mkdir -p bin node_modules/pi-code-mode \
    && cp /usr/local/bin/node bin/node \
    && cp -a /src/pi-code-mode/dist /src/pi-code-mode/LICENSE /src/pi-code-mode/package.json node_modules/pi-code-mode/ \
    && cp /usr/local/LICENSE node-LICENSE
COPY . /repo
RUN cp -a /payload payload \
    && uv build --wheel --python 3.12 \
    && uv run --no-project --python 3.12 --with /repo/dist/*.whl python tests/payload_smoke.py
FROM scratch AS artifact
COPY --from=build /repo/dist/ /
