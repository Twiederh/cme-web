FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY relay.py .
# Persistenter Speicher für den hochgeladenen APNs-Schlüssel (gehört "nobody")
RUN mkdir -p /data && chown nobody:nogroup /data
VOLUME /data
USER nobody
EXPOSE 8092 5072 8091
CMD ["python", "-u", "relay.py"]
