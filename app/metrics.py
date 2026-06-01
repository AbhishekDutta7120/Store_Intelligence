"""
metrics.py — GET /stores/{store_id}/metrics
Real-time. Excludes staff. Handles zero-purchase stores.
"""
from fastapi import APIRouter, HTTPException
from datetime import datetime, timezone, timedelta
from app.models import StoreMetrics, ZoneDwell
from app.database import get_conn

router = APIRouter()
WINDOW_HOURS = 8760


@router.get("/stores/{store_id}/metrics", response_model=StoreMetrics)
def get_metrics(store_id: str):
    now    = datetime.now(timezone.utc)
    since  = (now - timedelta(hours=WINDOW_HOURS)).strftime("%Y-%m-%dT%H:%M:%SZ")
    now_s  = now.strftime("%Y-%m-%dT%H:%M:%SZ")

    with get_conn() as conn:
        # Check store exists
        row = conn.execute(
            "SELECT COUNT(*) as c FROM events WHERE store_id=? AND is_staff=0", (store_id,)
        ).fetchone()
        if row["c"] == 0:
            # Return zeroed metrics rather than 404 — handles zero-traffic correctly
            return _zero_metrics(store_id, since, now_s)

        # Unique visitors (distinct visitor_ids with an ENTRY event, non-staff)
        unique_visitors = conn.execute("""
            SELECT COUNT(DISTINCT visitor_id) as cnt
            FROM events
            WHERE store_id=? AND is_staff=0 AND event_type IN ('ENTRY', 'REENTRY')
              AND timestamp >= ?
        """, (store_id, since)).fetchone()["cnt"]

        # Conversion: visitors who were in BILLING zone within 5-min before any transaction
        # We approximate by counting distinct visitor_ids with BILLING_QUEUE_JOIN events
        billing_visitors = conn.execute("""
            SELECT COUNT(DISTINCT visitor_id) as cnt
            FROM events
            WHERE store_id=? AND is_staff=0
              AND event_type IN ('BILLING_QUEUE_JOIN','ZONE_ENTER')
              AND zone_id='BILLING'
              AND timestamp >= ?
        """, (store_id, since)).fetchone()["cnt"]

        conversion_rate = (billing_visitors / unique_visitors) if unique_visitors > 0 else 0.0

        # Average dwell time across all zones
        avg_dwell = conn.execute("""
            SELECT AVG(dwell_ms) as avg
            FROM events
            WHERE store_id=? AND is_staff=0 AND event_type='ZONE_DWELL'
              AND timestamp >= ?
        """, (store_id, since)).fetchone()["avg"] or 0.0

        # Current queue depth (most recent queue_depth in billing zone)
        queue_row = conn.execute("""
            SELECT queue_depth FROM events
            WHERE store_id=? AND zone_id='BILLING' AND queue_depth IS NOT NULL
            ORDER BY timestamp DESC LIMIT 1
        """, (store_id,)).fetchone()
        queue_depth = queue_row["queue_depth"] if queue_row else 0

        # Abandonment rate
        abandoned = conn.execute("""
            SELECT COUNT(DISTINCT visitor_id) as cnt
            FROM events
            WHERE store_id=? AND is_staff=0
              AND event_type='BILLING_QUEUE_ABANDON'
              AND timestamp >= ?
        """, (store_id, since)).fetchone()["cnt"]
        abandonment_rate = (abandoned / billing_visitors) if billing_visitors > 0 else 0.0

        # Per-zone dwell averages
        zone_rows = conn.execute("""
            SELECT zone_id,
                   AVG(dwell_ms)   as avg_dwell,
                   COUNT(*)        as visit_count
            FROM events
            WHERE store_id=? AND is_staff=0 AND event_type='ZONE_DWELL'
              AND zone_id IS NOT NULL AND timestamp >= ?
            GROUP BY zone_id
        """, (store_id, since)).fetchall()

        zone_dwells = [
            ZoneDwell(zone_id=r["zone_id"], avg_dwell_ms=r["avg_dwell"], visit_count=r["visit_count"])
            for r in zone_rows
        ]

    return StoreMetrics(
        store_id        = store_id,
        unique_visitors = unique_visitors,
        conversion_rate = round(conversion_rate, 4),
        avg_dwell_ms    = round(avg_dwell, 2),
        queue_depth     = queue_depth or 0,
        abandonment_rate= round(abandonment_rate, 4),
        zone_dwells     = zone_dwells,
        window_start    = since,
        window_end      = now_s,
    )


def _zero_metrics(store_id, since, now_s):
    return StoreMetrics(
        store_id=store_id, unique_visitors=0, conversion_rate=0.0,
        avg_dwell_ms=0.0, queue_depth=0, abandonment_rate=0.0,
        zone_dwells=[], window_start=since, window_end=now_s,
    )
