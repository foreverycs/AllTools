FROM python:3.12-slim

WORKDIR /app

# Headless LibreOffice for Word → PDF (+ CJK fonts for Chinese docs).
# python:3.12-slim is Debian-based; packages come from the distro mirror.
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        libreoffice-writer \
        libreoffice-java-common \
        default-jre-headless \
        fonts-dejavu-core \
        fonts-liberation \
        fonts-noto-cjk \
        fonts-wqy-zenhei \
        fontconfig \
        ca-certificates \
    # Prefer the real binary path for LIBREOFFICE_PATH (soffice is usually a symlink).
RUN set -eux; \
    if [ -x /usr/bin/soffice ]; then echo /usr/bin/soffice > /etc/libreoffice-path; \
    elif [ -x /usr/bin/libreoffice ]; then echo /usr/bin/libreoffice > /etc/libreoffice-path; \
    else echo "LibreOffice binary not found" >&2; exit 1; fi; \
    LO="$(cat /etc/libreoffice-path)"; \
    "$LO" --version

ENV HOME=/tmp \
    SAL_USE_VCLPLUGIN=svp \
    PYTHONUNBUFFERED=1
# Resolved at runtime via converter PATH lookup; also export for clarity.
ENV LIBREOFFICE_PATH=/usr/bin/soffice

COPY requirements.txt .
RUN pip install --no-cache-dir -i https://mirrors.aliyun.com/pypi/simple/ --trusted-host mirrors.aliyun.com -r requirements.txt

COPY . .

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=10s --start-period=40s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=5)"

CMD ["python", "-m", "uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8000"]
