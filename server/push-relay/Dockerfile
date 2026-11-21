FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY relay.py .
USER nobody
EXPOSE 8090 5070
CMD ["python", "-u", "relay.py"]
