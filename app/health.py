"""
health.py — GET /health
Returns service status and STALE_FEED warning if >10 min since last event.
"""
from fastapi import APIRouter
from datetime import datetime, timezone, timedelta
from app.models import HealthResponse, StoreHealth
from app.database import get_conn

router = APIRouter()
STALE_THRESHOLD_MIN = 10


@router.get("/health", response_model=HealthResponse)
def health_check():
    now = datetime.now(timezone.utc)
    stores: list[StoreHealth] = []

    with get_conn() as conn:
        rows = conn.execute(
            "SELECT store_id, last_event_ts FROM health_log"
        ).fetchall()

    if not rows:
        return HealthResponse(status="OK", stores=[], version="1.0.0")

    overall_ok = True
    for row in rows:
        last_ts_str = row["last_event_ts"]
        if not last_ts_str:
            stores.append(StoreHealth(
                store_id=row["store_id"], status="NO_DATA",
                last_event_ts=None, lag_minutes=None,
            ))
            continue

        last_ts  = datetime.strptime(last_ts_str, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
        lag_min  = (now - last_ts).total_seconds() / 60
        status   = "STALE_FEED" if lag_min > STALE_THRESHOLD_MIN else "OK"
        if status != "OK":
            overall_ok = False

        stores.append(StoreHealth(
            store_id      = row["store_id"],
            status        = status,
            last_event_ts = last_ts_str,
            lag_minutes   = round(lag_min, 2),
        ))

    return HealthResponse(
        status  = "OK" if overall_ok else "DEGRADED",
        stores  = stores,
        version = "1.0.0",
    )
