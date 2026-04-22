# docgen sandbox — LibreOffice + Python + Node + CJK fonts.
# Used by apps that need to render / critic / recalc office files.
#
# Build:  docker build -f docker/sandbox.Dockerfile -t ai-gateway/docgen-sandbox:latest .
# Run:    docker run --rm --network=none -v "$PWD":/work ai-gateway/docgen-sandbox:latest \
#         bash -lc "python3 -c 'import docx; print(docx.__version__)'"
FROM ubuntu:24.04

ENV DEBIAN_FRONTEND=noninteractive \
    LANG=C.UTF-8 \
    LC_ALL=C.UTF-8 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

# System packages: LibreOffice (headless rendering + recalc + PDF convert),
# Poppler (pdftoppm for slide/page thumbnails), Tesseract (OCR fallback),
# Noto CJK fonts (SKILL canary: "東京 π √ 🚀 αβγ 测试").
RUN apt-get update \
 && apt-get install -y --no-install-recommends \
        python3 python3-pip python3-venv \
        nodejs npm \
        libreoffice-core libreoffice-writer libreoffice-impress libreoffice-calc \
        poppler-utils \
        tesseract-ocr tesseract-ocr-chi-sim tesseract-ocr-chi-tra \
        fonts-noto-cjk fonts-dejavu fonts-liberation \
        ca-certificates curl unzip \
 && rm -rf /var/lib/apt/lists/*

# Python deps — mirrors apps/assistant-service runtime but pinned looser
# here so we can rebuild without full repo lockfile.
RUN pip3 install --break-system-packages \
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

# Node deps — docx-js + pptxgenjs are the Skill-recommended libraries
# for from-scratch generation.
RUN npm install -g --unsafe-perm \
        docx@9.1.0 \
        pptxgenjs@3.12.0 \
        pdf-lib@1.17.1 \
        pdfjs-dist@4.7.76

WORKDIR /work

CMD ["bash", "-lc", "echo docgen sandbox ready"]
