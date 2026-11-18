# Aufzeichnungsserver (SIPREC) fuer CUBE: nimmt die vom Router kopierten Sprachstroeme an
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 TZ=Europe/Berlin
RUN apt-get update \
 && apt-get install -y --no-install-recommends ffmpeg tzdata \
 && rm -rf /var/lib/apt/lists/* \
 && groupadd --system --gid 10001 rec && useradd --system --uid 10001 --gid 10001 --create-home rec \
 && mkdir -p /recordings
COPY recorder.py /app/recorder.py
EXPOSE 5070/udp 5070/tcp 10100-10139/udp
HEALTHCHECK --interval=60s --timeout=5s --start-period=30s \
  CMD python -c "import json,time,sys; s=json.load(open('/recordings/.status.json')); sys.exit(time.time()-s['time']>90)"
# Volume gehoert dem Benutzer 10001 (wie cme-web), damit die Oberflaeche Aufnahmen loeschen kann
CMD ["/bin/sh", "-c", "chown 10001:10001 /recordings && chmod 775 /recordings && exec setpriv --reuid=10001 --regid=10001 --clear-groups python /app/recorder.py"]
