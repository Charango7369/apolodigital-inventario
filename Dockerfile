FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

ENV PYTHONPATH=/app

# Diagnóstico: salta alembic temporalmente
CMD ["sh", "-c", "echo PORT=$PORT && uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8000} --log-level debug"]