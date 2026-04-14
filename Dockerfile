FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

ENV PYTHONPATH=/app

CMD alembic upgrade head && echo "=== MIGRACION OK ===" && uvicorn app.main:app --host 0.0.0.0 --port $PORT --log-level debug
