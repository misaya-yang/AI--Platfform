# syntax=docker/dockerfile:1.7
# docgen sandbox — LibreOffice + Python + Node + CJK fonts.
# Used by apps that need to render / critic / recalc office files.
#
# Build:  docker build -f docker/sandbox.Dockerfile -t ai-gateway-docgen-sandbox:2.0.0 .
# Run:    docker run --rm --network=none -v "$PWD":/work ai-gateway-docgen-sandbox:2.0.0 \
#         bash -lc "python3 -c 'import docx; print(docx.__version__)'"
ARG UBUNTU_BASE_IMAGE=ubuntu:24.04
ARG NODE_BASE_IMAGE=node:22-bookworm-slim

FROM ${NODE_BASE_IMAGE} AS node-runtime

ARG NPM_REGISTRY=https://registry.npmjs.org

# Build the JavaScript toolchain on the official multi-arch Node image. Copying
# this stage avoids Ubuntu's npm package, which otherwise pulls hundreds of
# distribution-level Node development packages into the runtime image.
RUN --mount=type=cache,id=ai-gateway-npm,target=/root/.npm,sharing=locked \
    npm config set registry ${NPM_REGISTRY} \
 && npm install -g --unsafe-perm \
        docx@9.1.0 \
        pptxgenjs@3.12.0 \
        pdf-lib@1.17.1 \
        pdfjs-dist@4.7.76

FROM ${UBUNTU_BASE_IMAGE}

ARG APP_VERSION=2.0.0
ARG VCS_REF=unknown
# Ubuntu's minimal base does not include CA certificates yet. Bootstrap apt
# over the distro's signed HTTP repositories; apt still verifies InRelease and
# package signatures, and ca-certificates is installed in this same layer.
ARG UBUNTU_MIRROR=http://archive.ubuntu.com/ubuntu
ARG UBUNTU_PORTS_MIRROR=http://ports.ubuntu.com/ubuntu-ports
ARG PIP_INDEX_URL=https://pypi.org/simple
ARG PIP_TRUSTED_HOST=""
ARG SANDBOX_UID=10001
LABEL org.opencontainers.image.title="AI Gateway Document Sandbox" \
      org.opencontainers.image.source="https://github.com/misaya-yang/AI--Platfform" \
      org.opencontainers.image.version="$APP_VERSION" \
      org.opencontainers.image.revision="$VCS_REF"

ENV DEBIAN_FRONTEND=noninteractive \
    LANG=C.UTF-8 \
    LC_ALL=C.UTF-8 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# System packages: LibreOffice (headless rendering + recalc + PDF convert),
# Poppler (pdftoppm for slide/page thumbnails), Tesseract (OCR fallback),
# Noto CJK fonts (SKILL canary: "東京 π √ 🚀 αβγ 测试").
RUN --mount=type=cache,id=ai-gateway-ubuntu-apt,target=/var/cache/apt,sharing=locked \
    --mount=type=cache,id=ai-gateway-ubuntu-lists,target=/var/lib/apt/lists,sharing=locked \
    rm -f /etc/apt/apt.conf.d/docker-clean \
 && sed -i \
        -e "s|http://archive.ubuntu.com/ubuntu|${UBUNTU_MIRROR}|g" \
        -e "s|http://ports.ubuntu.com/ubuntu-ports|${UBUNTU_PORTS_MIRROR}|g" \
        /etc/apt/sources.list.d/ubuntu.sources \
 && apt-get -o Acquire::Retries=5 update \
 && apt-get -o Acquire::Retries=5 install -y --no-install-recommends \
        python3 python3-pip python3-venv \
        libreoffice-core libreoffice-writer libreoffice-impress libreoffice-calc \
        poppler-utils \
        tesseract-ocr tesseract-ocr-chi-sim tesseract-ocr-chi-tra \
        fonts-noto-cjk fonts-dejavu fonts-liberation \
        ca-certificates curl unzip

# Python deps — mirrors apps/assistant-service runtime but pinned looser
# here so we can rebuild without full repo lockfile.
RUN --mount=type=cache,id=ai-gateway-pip,target=/root/.cache/pip,sharing=locked \
    pip3 install --break-system-packages \
        --index-url ${PIP_INDEX_URL} \
        ${PIP_TRUSTED_HOST:+--trusted-host ${PIP_TRUSTED_HOST}} \
        python-docx==1.1.2 \
        python-pptx==0.6.23 \
        openpyxl==3.1.5 \
        xlsxwriter==3.2.0 \
        reportlab==4.2.0 \
        weasyprint==62.3 \
        pypdf==4.3.0 \
        pdfplumber==0.11.4 \
        pypdfium2==4.30.0 \
        pdf2image==1.17.0 \
        pandas==2.2.2 \
        matplotlib==3.9.0 \
        pillow==10.4.0 \
        markdown==3.6 \
        markitdown==0.0.1a3 \
        docxtpl==0.18.0 \
        lxml==5.2.2 \
        pyyaml==6.0.2

# Node runtime + global doc generation packages from the dedicated build stage.
COPY --from=node-runtime /usr/local/ /usr/local/

WORKDIR /work

RUN useradd --uid "${SANDBOX_UID}" --create-home --user-group sandbox \
 && chown -R sandbox:sandbox /work
USER sandbox

CMD ["bash", "-lc", "echo docgen sandbox ready"]
