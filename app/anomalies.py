"""
anomalies.py — GET /stores/{store_id}/anomalies
Detects: queue spike, conversion drop, dead zone, no-traffic period.
"""
from fastapi import APIRouter
from datetime import datetime, timezone, timedelta
from app.models import AnomalyResponse, Anomaly
from app.database import get_conn

router = APIRouter()


@router.get("/stores/{store_id}/anomalies", response_model=AnomalyResponse)
def get_anomalies(store_id: str):
    now     = datetime.now(timezone.utc)
    now_s   = now.strftime("%Y-%m-%dT%H:%M:%SZ")
    since_1h  = (now - timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
    since_24h = (now - timedelta(hours=24)).strftime("%Y-%m-%dT%H:%M:%SZ")
    since_7d  = (now - timedelta(days=7)).strftime("%Y-%m-%dT%H:%M:%SZ")
    since_30m = (now - timedelta(minutes=30)).strftime("%Y-%m-%dT%H:%M:%SZ")

    anomalies = []

    with get_conn() as conn:
        # ── 1. BILLING_QUEUE_SPIKE ──────────────────────────────────────────
        queue_row = conn.execute("""
            SELECT MAX(queue_depth) as max_q FROM events
            WHERE store_id=? AND zone_id='BILLING'
              AND queue_depth IS NOT NULL AND timestamp >= ?
        """, (store_id, since_1h)).fetchone()
        max_q = queue_row["max_q"] or 0

        avg_queue = conn.execute("""
            SELECT AVG(queue_depth) as avg_q FROM events
            WHERE store_id=? AND zone_id='BILLING'
              AND queue_depth IS NOT NULL AND timestamp >= ?
        """, (store_id, since_7d)).fetchone()["avg_q"] or 0

        if max_q > 5:
            severity = "CRITICAL" if max_q > 8 else "WARN"
            anomalies.append(Anomaly(
                anomaly_type    = "BILLING_QUEUE_SPIKE",
                severity        = severity,
                description     = f"Billing queue reached {max_q} (7-day avg: {avg_queue:.1f})",
                suggested_action= "Open additional billing counter or redirect customers",
                detected_at     = now_s,
            ))

        # ── 2. CONVERSION_DROP ─────────────────────────────────────────────
        def conversion_rate(since):
            entries = conn.execute("""
                SELECT COUNT(DISTINCT visitor_id) FROM events
                WHERE store_id=? AND is_staff=0 AND event_type IN ('ENTRY', 'REENTRY')
                  AND timestamp >= ?
            """, (store_id, since)).fetchone()[0]
            billing = conn.execute("""
                SELECT COUNT(DISTINCT visitor_id) FROM events
                WHERE store_id=? AND is_staff=0
                  AND event_type IN ('BILLING_QUEUE_JOIN','ZONE_ENTER')
                  AND zone_id='BILLING' AND timestamp >= ?
            """, (store_id, since)).fetchone()[0]
            return billing / entries if entries > 0 else None

        today_rate = conversion_rate(since_24h)
        week_rate  = conversion_rate(since_7d)

        if today_rate is not None and week_rate and week_rate > 0:
            drop_pct = (week_rate - today_rate) / week_rate * 100
            if drop_pct > 30:
                severity = "CRITICAL" if drop_pct > 50 else "WARN"
                anomalies.append(Anomaly(
                    anomaly_type    = "CONVERSION_DROP",
                    severity        = severity,
                    description     = f"Conversion {today_rate:.1%} vs 7-day avg {week_rate:.1%} ({drop_pct:.0f}% drop)",
                    suggested_action= "Check floor staff availability and product placement",
                    detected_at     = now_s,
                ))

        # ── 3. DEAD_ZONE ───────────────────────────────────────────────────
        active_zones = {r[0] for r in conn.execute("""
            SELECT DISTINCT zone_id FROM events
            WHERE store_id=? AND is_staff=0 AND zone_id IS NOT NULL
              AND timestamp >= ?
        """, (store_id, since_30m)).fetchall()}

        all_zones = {r[0] for r in conn.execute("""
            SELECT DISTINCT zone_id FROM events
            WHERE store_id=? AND zone_id IS NOT NULL
        """, (store_id,)).fetchall()}

        dead_zones = all_zones - active_zones
        for zone in dead_zones:
            anomalies.append(Anomaly(
                anomaly_type    = "DEAD_ZONE",
                severity        = "INFO",
                description     = f"Zone '{zone}' had no customer visits in last 30 minutes",
                suggested_action= f"Consider moving promotional display to zone {zone}",
                detected_at     = now_s,
            ))

        # ── 4. NO_TRAFFIC ──────────────────────────────────────────────────
        recent_count = conn.execute("""
            SELECT COUNT(*) FROM events
            WHERE store_id=? AND is_staff=0 AND timestamp >= ?
        """, (store_id, since_30m)).fetchone()[0]

        total_count = conn.execute("""
            SELECT COUNT(*) FROM events WHERE store_id=? AND is_staff=0
        """, (store_id,)).fetchone()[0]

        if total_count > 0 and recent_count == 0:
            anomalies.append(Anomaly(
                anomaly_type    = "NO_TRAFFIC",
                severity        = "WARN",
                description     = "No customer events in the last 30 minutes",
                suggested_action= "Verify camera feeds are active; check store open hours",
                detected_at     = now_s,
            ))

    return AnomalyResponse(store_id=store_id, anomalies=anomalies)
