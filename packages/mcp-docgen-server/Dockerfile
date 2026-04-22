# syntax=docker/dockerfile:1.6
#
# mcp-docgen-server — production image.
# Includes LibreOffice (for xlsx formula recalc + pptx→pdf conversion),
# Noto CJK fonts (so Chinese / Japanese / Korean text renders instead of
# tofu boxes), poppler (pdf utilities used by the vision critic), and
# curl (healthcheck). Chromium is intentionally omitted — the vision
# critic uses the docgen.sandbox Node subprocess when it needs it; add
# chromium here if you enable the in-process rasterizer.

FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    LANG=C.UTF-8 \
    LC_ALL=C.UTF-8

# System deps
#   libreoffice-{core,impress,calc,writer}  headless converters
#   fontconfig + fonts-noto*                CJK-capable fallback fonts
#   poppler-utils                           pdftoppm / pdfinfo used by critic
#   curl                                    used by the healthcheck
#   locales                                 required for C.UTF-8 support
RUN apt-get update \
 && apt-get install -y --no-install-recommends \
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
COPY packages/mcp-docgen-server/tests/         ./tests/

# Install with the ``mcp`` extra which pulls in the transport stack
# (mcp, starlette, uvicorn). Dev extras are omitted from the prod image.
RUN pip install --no-cache-dir -e ".[mcp]"

# Run as an unprivileged user. /app is owned by root but only needs to be
# read by runtime — artifacts are written to a dedicated volume mount.
RUN useradd --system --create-home --shell /usr/sbin/nologin docgen \
 && mkdir -p /var/lib/docgen/artifacts \
 && chown -R docgen:docgen /var/lib/docgen
USER docgen

ENV MCP_TRANSPORT=sse \
    PORT=8765 \
    LOG_LEVEL=INFO \
    DOCGEN_ARTIFACT_ROOT=/var/lib/docgen/artifacts

VOLUME ["/var/lib/docgen/artifacts"]
EXPOSE 8765

HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
  CMD curl -fsS "http://127.0.0.1:${PORT}/health" || exit 1

CMD ["python", "-m", "mcp_docgen_server"]
