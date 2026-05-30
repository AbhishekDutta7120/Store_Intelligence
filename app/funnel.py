"""
funnel.py — GET /stores/{store_id}/funnel and /heatmap
Session-level deduplication: re-entries do not double-count a visitor.
"""
from fastapi import APIRouter
from datetime import datetime, timezone, timedelta
from app.models import FunnelResponse, FunnelStage, HeatmapResponse, HeatmapZone
from app.database import get_conn

router = APIRouter()
WINDOW_HOURS = 24


@router.get("/stores/{store_id}/funnel", response_model=FunnelResponse)
def get_funnel(store_id: str):
    now   = datetime.now(timezone.utc)
    since = (now - timedelta(hours=WINDOW_HOURS)).strftime("%Y-%m-%dT%H:%M:%SZ")

    with get_conn() as conn:
        # Unique visitors who entered (ENTRY events, non-staff, no re-entry double-count)
        entries = conn.execute("""
            SELECT COUNT(DISTINCT visitor_id) as cnt FROM events
            WHERE store_id=? AND is_staff=0
              AND event_type IN ('ENTRY', 'REENTRY') AND timestamp >= ?
        """, (store_id, since)).fetchone()["cnt"]

        # Visited at least one zone (ZONE_ENTER or ZONE_DWELL)
        zone_visitors = conn.execute("""
            SELECT COUNT(DISTINCT visitor_id) as cnt FROM events
            WHERE store_id=? AND is_staff=0
              AND event_type IN ('ZONE_ENTER','ZONE_DWELL')
              AND timestamp >= ?
        """, (store_id, since)).fetchone()["cnt"]

        # Reached billing zone
        billing_visitors = conn.execute("""
            SELECT COUNT(DISTINCT visitor_id) as cnt FROM events
            WHERE store_id=? AND is_staff=0
              AND event_type IN ('BILLING_QUEUE_JOIN','ZONE_ENTER')
              AND zone_id='BILLING' AND timestamp >= ?
        """, (store_id, since)).fetchone()["cnt"]

        # Completed purchase (approximation: billing visitor who did NOT abandon)
        abandoned_visitors = conn.execute("""
            SELECT COUNT(DISTINCT visitor_id) as cnt FROM events
            WHERE store_id=? AND is_staff=0
              AND event_type='BILLING_QUEUE_ABANDON' AND timestamp >= ?
        """, (store_id, since)).fetchone()["cnt"]
        purchases = max(0, billing_visitors - abandoned_visitors)

    stages_raw = [
        ("Entry",         entries),
        ("Zone visit",    zone_visitors),
        ("Billing queue", billing_visitors),
        ("Purchase",      purchases),
    ]

    stages = []
    for i, (name, count) in enumerate(stages_raw):
        prev = stages_raw[i - 1][1] if i > 0 else count
        drop = round((1 - count / prev) * 100, 2) if prev > 0 else 0.0
        stages.append(FunnelStage(stage=name, count=count, drop_off_pct=drop))

    return FunnelResponse(store_id=store_id, stages=stages)


@router.get("/stores/{store_id}/heatmap", response_model=HeatmapResponse)
def get_heatmap(store_id: str):
    now   = datetime.now(timezone.utc)
    since = (now - timedelta(hours=WINDOW_HOURS)).strftime("%Y-%m-%dT%H:%M:%SZ")

    with get_conn() as conn:
        rows = conn.execute("""
            SELECT zone_id,
                   COUNT(DISTINCT visitor_id)    as visit_freq,
                   AVG(dwell_ms)                 as avg_dwell
            FROM events
            WHERE store_id=? AND is_staff=0
              AND zone_id IS NOT NULL AND timestamp >= ?
            GROUP BY zone_id
        """, (store_id, since)).fetchall()

        total_sessions = conn.execute("""
            SELECT COUNT(DISTINCT visitor_id) as cnt FROM events
            WHERE store_id=? AND is_staff=0 AND timestamp >= ?
        """, (store_id, since)).fetchone()["cnt"]

    if not rows:
        return HeatmapResponse(store_id=store_id, zones=[])

    max_freq = max(r["visit_freq"] for r in rows) or 1

    zones = [
        HeatmapZone(
            zone_id         = r["zone_id"],
            visit_frequency = round((r["visit_freq"] / max_freq) * 100, 2),
            avg_dwell_ms    = round(r["avg_dwell"] or 0.0, 2),
            data_confidence = total_sessions >= 20,
        )
        for r in rows
    ]

    return HeatmapResponse(store_id=store_id, zones=zones)
