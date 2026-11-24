# hey-claude-relay on Alpine (musl). Every dependency installs from a wheel, so there is no
# compiler and no `apk add` here. Images are pinned by digest; Dependabot bumps tag and digest
# together (.github/dependabot.yml).

FROM python:3.13-alpine@sha256:9ef1a599f863231c3d91e941389bfdc4a2d6070557243d54df1a4e57015b960e AS build
COPY --from=ghcr.io/astral-sh/uv:0.13.9@sha256:d3b601f0c7b118bc1753febf7c41cc80981df678445e0254284eecf84db79a7d /uv /bin/uv
# Never fetch another Python: the image's own is the one the final stage has.
ENV UV_PYTHON_DOWNLOADS=never UV_LINK_MODE=copy UV_COMPILE_BYTECODE=1
WORKDIR /app
COPY pyproject.toml uv.lock LICENSE ./
COPY src ./src
# --locked: fail if uv.lock does not match pyproject.toml. --no-editable: the venv holds the
# package itself, so the final stage needs nothing but /app/.venv.
RUN uv sync --locked --no-dev --no-editable

FROM python:3.13-alpine@sha256:9ef1a599f863231c3d91e941389bfdc4a2d6070557243d54df1a4e57015b960e
RUN adduser -D -u 10001 relay
COPY --from=build /app/.venv /app/.venv
ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    RELAY_CONFIG=/config/relay.toml
# The commit this image was built from, reported by /healthz.
ARG GIT_COMMIT=""
ENV RELAY_COMMIT=$GIT_COMMIT
USER relay
EXPOSE 8787
# The image has no curl; Python is already here.
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8787/healthz', timeout=3)"]
ENTRYPOINT ["hey-claude-relay"]
CMD ["serve", "--bind", "0.0.0.0", "--port", "8787"]
