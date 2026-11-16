FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    CME_LISTEN=0.0.0.0 \
    CME_WEB_PORT=8080

WORKDIR /app
COPY requirements.txt .
RUN pip install -r requirements.txt \
 && useradd --system --uid 10001 --create-home cme

COPY cme_web.py auth.py ./
COPY static ./static

USER cme
EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8080/healthz', timeout=4)"

CMD ["python", "cme_web.py"]
