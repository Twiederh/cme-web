FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY relay.py .
# Gemeinsames Volume mit cme-web (APNs-Schluessel, Secret, Status): gleiche Benutzer-ID wie dort (10001)
RUN useradd --system --uid 10001 --no-create-home relay \
 && mkdir -p /data && chown relay /data
VOLUME /data
USER relay
EXPOSE 8092 5072
CMD ["python", "-u", "relay.py"]
