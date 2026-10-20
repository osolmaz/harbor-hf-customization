# Derived Harbor-HF parent worker for Anonbench1 runs: the reviewed parent image
# plus the pinned native Anonbench1 adapters. The base image is pinned by digest
# and never replaced. Build from the repository root:
#
#   docker buildx build --platform linux/amd64 \
#     -f build/anonbench1-parent/Dockerfile --output type=local,dest=dist/anonbench1-parent .
#
# The operator points HARBOR_HF_PARENT_IMAGE at the pushed image digest. The
# wheel carries no credentials and no task data.
FROM ghcr.io/osolmaz/harbor-hf-customization/anonbench1-parent@sha256:31a85f921a974a8897102510cb094ecee8b44ca6c06a920845491c361c85e1db

COPY packages/anonbench1-adapters/dist/harbor_anonbench1_adapters-0.1.0rc12-py3-none-any.whl /tmp/

RUN uv pip install --python /opt/harbor-hf-parent \
        /tmp/harbor_anonbench1_adapters-0.1.0rc12-py3-none-any.whl \
    && python -c "from anonbench1_adapters import Anonbench1ClaudeCode, Anonbench1Codex, Anonbench1Hermes, Anonbench1Pi" \
    && rm /tmp/harbor_anonbench1_adapters-0.1.0rc12-py3-none-any.whl

# Exact same-source bundle, installed and inspected before image publication.
COPY build/openclaw-bundle/runtime.tar.gz /opt/harbor-hf-openclaw/runtime.tar.gz
COPY build/openclaw-bundle/runtime.tar.gz.sha256 /opt/harbor-hf-openclaw/runtime.tar.gz.sha256
COPY build/openclaw-bundle/runtime.json /opt/harbor-hf-openclaw/runtime.json
RUN cd /opt/harbor-hf-openclaw \
    && echo "$(cat runtime.tar.gz.sha256)  runtime.tar.gz" | sha256sum -c - \
    && python -c "from anonbench1_adapters.native_openclaw import Anonbench1OpenClaw"
