FROM python:3.11-slim

RUN apt-get update && apt-get install -y ffmpeg && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY relay.py .

CMD gunicorn relay:app \
    --bind 0.0.0.0:${PORT:-8080} \
    --timeout 700 \
    --worker-class gthread \
    --workers 1 \
    --threads 8 \
    --log-level info \
    --access-logfile -
