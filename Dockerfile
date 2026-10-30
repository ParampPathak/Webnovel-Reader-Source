FROM python:3.12-slim

WORKDIR /app

# ffmpeg compresses Piper's temporary WAV to a much smaller MP3 so long queues do
# not burn through the Docker VM's disk space.
RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg espeak-ng \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY *.py ./
COPY docker-entrypoint.sh ./
COPY templates/ templates/
COPY static/ static/
COPY tools/ tools/
COPY tests/ tests/

RUN mkdir -p /data/audio/segments /data/piper_voices

EXPOSE 5000

ENTRYPOINT ["sh", "/app/docker-entrypoint.sh"]

CMD ["gunicorn", "--bind", "0.0.0.0:5000", "--workers", "1", "--threads", "2", "--timeout", "120", "--graceful-timeout", "30", "--access-logfile", "-", "app:create_app()"]
