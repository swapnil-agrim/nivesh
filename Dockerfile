# Nivesh image: Python 3.12, uv-locked deps, `age` for backups, non-root, no secrets baked in.
FROM python:3.12-slim-bookworm

RUN apt-get update \
    && apt-get install -y --no-install-recommends age \
    && rm -rf /var/lib/apt/lists/*
COPY --from=ghcr.io/astral-sh/uv:0.8.13 /uv /usr/local/bin/uv

# Fixed uid so the mounted /data volume can be chowned to it (docs/deploy/vm.md).
RUN useradd --uid 10001 --create-home --shell /usr/sbin/nologin nivesh \
    && mkdir /data && chown nivesh /data && chmod 700 /data

WORKDIR /app
COPY pyproject.toml uv.lock README.md ./
COPY nivesh_core nivesh_core
COPY nivesh_engine nivesh_engine
COPY nivesh_adapters nivesh_adapters
COPY nivesh_mcp nivesh_mcp
COPY nivesh_agents nivesh_agents
COPY nivesh_cli nivesh_cli
COPY .claude .claude
COPY config config
RUN uv sync --frozen --no-dev

# Container config points data_dir at the /data volume; secrets arrive as environment variables.
RUN mkdir /cfg \
    && sed 's|^data_dir:.*|data_dir: /data|' config/nivesh.yaml > /cfg/nivesh.yaml \
    && cp config/profile.yaml /cfg/profile.yaml
ENV PATH="/app/.venv/bin:$PATH" \
    NIVESH_CONFIG_DIR=/cfg \
    PYTHONDONTWRITEBYTECODE=1

USER nivesh
VOLUME /data
ENTRYPOINT ["nivesh"]
CMD ["--help"]
