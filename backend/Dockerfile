FROM python:3.12-slim

WORKDIR /app

COPY backend/requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt

COPY backend/app /app/app
COPY frontend /app/frontend

# DATABASE_URL hat bewusst KEINEN Standardwert - die Anwendung bricht ohne
# die Variable mit einer klaren Meldung ab, statt scheinbar zu laufen.
ENV DATA_DIR=/app/data \
    CONFIG_PATH=/app/config/inverters.json \
    FRONTEND_DIR=/app/frontend \
    POLL_INTERVAL_SECONDS=15 \
    TIMEZONE=Europe/Berlin \
    AUTO_IMPORT_HISTORY=true \
    AUTO_IMPORT_DAYS=35 \
    GRID_POWER_INVERTED=false

EXPOSE 8000

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
