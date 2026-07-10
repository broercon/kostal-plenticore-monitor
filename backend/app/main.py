"""FastAPI-Anwendung: dient die REST-API und das statische Frontend aus."""
from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone

from fastapi import FastAPI, Query
from fastapi.staticfiles import StaticFiles
from sqlalchemy import select

from .aggregation import aggregate_per_device, combine_devices
from .config import settings
from .database import SessionLocal, init_db
from .models import Reading
from .poller import poller
from .schemas import DeviceOut, HistoryPoint, ReadingOut, SummaryOut

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    poller.start()
    yield
    await poller.stop()


app = FastAPI(title="Kostal Plenticore Monitor", lifespan=lifespan)


@app.get("/api/devices", response_model=list[DeviceOut])
def get_devices() -> list[DeviceOut]:
    return [
        DeviceOut(id=cfg.id, name=cfg.name, host=cfg.host) for cfg in settings.inverters
    ]


@app.get("/api/readings/latest", response_model=list[ReadingOut])
def get_latest() -> list[ReadingOut]:
    return [ReadingOut(**reading) for reading in poller.latest.values()]


@app.get("/api/readings/history", response_model=list[HistoryPoint])
def get_history(
    device_id: str | None = Query(default=None, description="Leer = alle Geraete summiert"),
    hours: float = Query(default=24, ge=0.1, le=24 * 90),
    bucket_minutes: float = Query(default=5, ge=1, le=1440),
) -> list[HistoryPoint]:
    since = datetime.now(timezone.utc) - timedelta(hours=hours)
    bucket_seconds = int(bucket_minutes * 60)

    session = SessionLocal()
    try:
        stmt = select(Reading).where(Reading.timestamp >= since).order_by(Reading.timestamp)
        if device_id:
            stmt = stmt.where(Reading.device_id == device_id)
        rows = list(session.scalars(stmt))
    finally:
        session.close()

    per_device = aggregate_per_device(rows, bucket_seconds)

    if device_id:
        buckets = per_device.get(device_id, {})
    else:
        buckets = combine_devices(per_device)

    points = [
        HistoryPoint(
            timestamp=datetime.fromtimestamp(bk, tz=timezone.utc),
            **values,
        )
        for bk, values in sorted(buckets.items())
    ]
    return points


@app.get("/api/readings/today-summary", response_model=list[SummaryOut])
def get_today_summary() -> list[SummaryOut]:
    summaries = []
    for cfg in settings.inverters:
        reading = poller.latest.get(cfg.id)
        if reading is None:
            summaries.append(SummaryOut(device_id=cfg.id, device_name=cfg.name))
            continue
        summaries.append(
            SummaryOut(
                device_id=cfg.id,
                device_name=cfg.name,
                yield_day_kwh=reading.get("yield_day_kwh"),
                home_consumption_day_kwh=reading.get("home_consumption_day_kwh"),
                energy_grid_day_kwh=reading.get("energy_grid_day_kwh"),
                as_of=reading.get("timestamp"),
            )
        )
    return summaries


# Statisches Frontend (index.html, app.js, style.css) unter "/" ausliefern.
if settings.frontend_dir.exists():
    app.mount(
        "/", StaticFiles(directory=str(settings.frontend_dir), html=True), name="frontend"
    )
