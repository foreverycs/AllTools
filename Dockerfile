# Base image: default uses a China-friendly mirror of Docker Hub library/python.
# Override if needed, e.g.:
#   docker compose build --build-arg PYTHON_IMAGE=python:3.12-slim
#   docker compose build --build-arg PYTHON_IMAGE=registry.cn-hangzhou.aliyuncs.com/library/python:3.12-slim
#
# Size knobs (build args):
#   WITH_JAVA=0  (default)  skip the JRE. LibreOffice converts .docx/.doc → PDF
#                          without Java for ordinary documents. Enable only if
#                          you hit docs needing the Java-based filter:
#                            docker build --build-arg WITH_JAVA=1 .
#   WITH_OCR=1   (default)  install Tesseract + chi_sim/eng for PDF→Word OCR.
#                          Set 0 to drop OCR and shrink further.
ARG PYTHON_IMAGE=docker.m.daocloud.io/library/python:3.12-slim
FROM ${PYTHON_IMAGE}

WORKDIR /app

ARG WITH_JAVA=0
ARG WITH_OCR=1

# Debian apt → Aliyun (faster on mainland / Alibaba Cloud ECS).
# bookworm = current python:3.12-slim base; adjust if the base tag changes.
RUN set -eux; \
    if [ -f /etc/apt/sources.list.d/debian.sources ]; then \
      sed -i \
        -e 's|deb.debian.org|mirrors.aliyun.com|g' \
        -e 's|security.debian.org|mirrors.aliyun.com|g' \
        /etc/apt/sources.list.d/debian.sources; \
    fi; \
    if [ -f /etc/apt/sources.list ]; then \
      sed -i \
        -e 's|deb.debian.org|mirrors.aliyun.com|g' \
        -e 's|security.debian.org|mirrors.aliyun.com|g' \
        /etc/apt/sources.list; \
    fi

# Headless LibreOffice for Word → PDF (+ CJK fonts for Chinese docs).
# writer-nogui is smaller than full libreoffice-writer (no GUI stack).
# OCR (optional): tesseract + chi_sim/eng language packs for PDF→Word OCR.
RUN set -eux; \
    apt-get update; \
    apt-get install -y --no-install-recommends \
        libreoffice-writer-nogui \
        fonts-dejavu-core \
        fonts-liberation \
        fonts-noto-cjk \
        fontconfig \
        ca-certificates; \
    if [ "$WITH_JAVA" = "1" ]; then \
      apt-get install -y --no-install-recommends \
        libreoffice-java-common \
        default-jre-headless; \
    fi; \
    if [ "$WITH_OCR" = "1" ]; then \
      apt-get install -y --no-install-recommends \
        tesseract-ocr \
        tesseract-ocr-chi-sim \
        tesseract-ocr-eng; \
    fi; \
    fc-cache -f; \
    # Drop orphaned packages + all apt cache so the layer stays small.
    apt-get autoremove -y; \
    apt-get clean; \
    rm -rf /var/lib/apt/lists/* /var/cache/apt/archives/* /tmp/*; \
    if [ -x /usr/bin/soffice ]; then LO=/usr/bin/soffice; \
    elif [ -x /usr/bin/libreoffice ]; then LO=/usr/bin/libreoffice; \
    else echo "LibreOffice binary not found" >&2; exit 1; fi; \
    echo "$LO" > /etc/libreoffice-path; \
    "$LO" --version; \
    if [ "$WITH_OCR" = "1" ]; then tesseract --version; fi

ENV HOME=/tmp \
    SAL_USE_VCLPLUGIN=svp \
    PYTHONUNBUFFERED=1 \
    LIBREOFFICE_PATH=/usr/bin/soffice \
    PIP_INDEX_URL=https://mirrors.aliyun.com/pypi/simple/ \
    PIP_TRUSTED_HOST=mirrors.aliyun.com

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=10s --start-period=40s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=5)"

# Single worker: async jobs live in process memory (see README / JOBS_BACKEND).
CMD ["python", "-m", "uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1"]
