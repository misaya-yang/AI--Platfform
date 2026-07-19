# syntax=docker/dockerfile:1.7
#
# mcp-docgen-server — production image.
# Includes LibreOffice (for xlsx formula recalc + pptx→pdf conversion),
# Noto CJK fonts (so Chinese / Japanese / Korean text renders instead of
# tofu boxes), poppler (pdf utilities used by the vision critic), and
# curl (healthcheck). Chromium is intentionally omitted — the vision
# critic uses the docgen.sandbox Node subprocess when it needs it; add
# chromium here if you enable the in-process rasterizer.

ARG PYTHON_BASE_IMAGE=python:3.13-slim-bookworm

FROM ${PYTHON_BASE_IMAGE}

ARG APP_VERSION=2.0.0
ARG VCS_REF=unknown
ARG DEBIAN_MIRROR=https://deb.debian.org
ARG PIP_INDEX_URL=https://pypi.org/simple
ARG PIP_TRUSTED_HOST=""
LABEL org.opencontainers.image.title="AI Gateway MCP Document Service" \
      org.opencontainers.image.source="https://github.com/misaya-yang/AI--Platfform" \
      org.opencontainers.image.version="$APP_VERSION" \
      org.opencontainers.image.revision="$VCS_REF"

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    LANG=C.UTF-8 \
    LC_ALL=C.UTF-8

# System deps
#   libreoffice-{core,impress,calc,writer}  headless converters
#   fontconfig + fonts-noto*                CJK-capable fallback fonts
#   poppler-utils                           pdftoppm / pdfinfo used by critic
#   curl                                    used by the healthcheck
#   locales                                 required for C.UTF-8 support
RUN sed -i "s|http://deb.debian.org|${DEBIAN_MIRROR}|g" /etc/apt/sources.list.d/debian.sources \
 && apt-get -o Acquire::Retries=5 update \
 && apt-get -o Acquire::Retries=5 install -y --no-install-recommends \
      libreoffice-core \
      libreoffice-impress \
      libreoffice-calc \
      libreoffice-writer \
      fontconfig \
      fonts-noto \
      fonts-noto-cjk \
      fonts-noto-cjk-extra \
      poppler-utils \
      curl \
      ca-certificates \
      locales \
 && rm -rf /var/lib/apt/lists/* \
 && fc-cache -f

WORKDIR /app

# Copy package sources. Build context is the repository root:
#   docker build -f packages/mcp-docgen-server/Dockerfile -t mcp-docgen .
COPY packages/mcp-docgen-server/pyproject.toml ./pyproject.toml
COPY packages/mcp-docgen-server/README.md      ./README.md
COPY packages/mcp-docgen-server/src/           ./src/

# Install with the ``mcp`` extra which pulls in the transport stack
# (mcp, starlette, uvicorn). Dev extras are omitted from the prod image.
RUN --mount=type=cache,id=ai-gateway-pip,target=/root/.cache/pip,sharing=locked \
    pip install ".[mcp]" \
    --index-url ${PIP_INDEX_URL} \
    ${PIP_TRUSTED_HOST:+--trusted-host ${PIP_TRUSTED_HOST}}

# Run as an unprivileged user. /app is owned by root but only needs to be
# read by runtime — artifacts are written to a dedicated volume mount.
RUN useradd --system --create-home --shell /usr/sbin/nologin docgen \
 && mkdir -p /var/lib/docgen/artifacts \
 && chown -R docgen:docgen /var/lib/docgen
USER docgen

ENV MCP_TRANSPORT=http \
    PORT=8765 \
    LOG_LEVEL=INFO \
    PYTHONPATH=/app/src \
    DOCGEN_ARTIFACT_ROOT=/var/lib/docgen/artifacts

VOLUME ["/var/lib/docgen/artifacts"]
EXPOSE 8765

HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
  CMD curl -fsS "http://127.0.0.1:${PORT}/health" || exit 1

CMD ["python", "-m", "mcp_docgen_server"]
