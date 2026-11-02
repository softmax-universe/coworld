FROM python:3.12-slim

WORKDIR /app
RUN pip install --no-cache-dir websockets
COPY player.py run_stats.py /app/

CMD ["python", "/app/player.py"]
