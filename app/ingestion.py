"""
ingestion.py — POST /events/ingest
Idempotent by event_id. Partial success on malformed events.
"""
from fastapi import APIRouter
from datetime import datetime, timezone
from app.models import IngestRequest, IngestResponse, Event
from app.database import get_conn

router = APIRouter()


@router.post("/events/ingest", response_model=IngestResponse)
def ingest_events(body: IngestRequest):
    accepted  = 0
    rejected  = 0
    duplicate = 0
    errors    = []

    now_str = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    with get_conn() as conn:
        for i, event in enumerate(body.events):
            try:
                _insert_event(conn, event, now_str)
                accepted += 1
            except DuplicateEvent:
                duplicate += 1
            except Exception as exc:
                rejected += 1
                errors.append({"index": i, "event_id": getattr(event, "event_id", None),
                                "error": str(exc)})

        # Update health log for each store seen
        stores_seen = {e.store_id for e in body.events}
        for store_id in stores_seen:
            conn.execute("""
                INSERT INTO health_log(store_id, last_event_ts, updated_at)
                VALUES (?, ?, ?)
                ON CONFLICT(store_id) DO UPDATE SET
                    last_event_ts = excluded.last_event_ts,
                    updated_at    = excluded.updated_at
                WHERE excluded.last_event_ts > health_log.last_event_ts
                   OR health_log.last_event_ts IS NULL
            """, (store_id, _latest_ts_for_store(body.events, store_id), now_str))

    return IngestResponse(
        accepted  = accepted,
        rejected  = rejected,
        duplicate = duplicate,
        errors    = errors,
    )


class DuplicateEvent(Exception):
    pass


def _insert_event(conn, event: Event, ingested_at: str):
    try:
        conn.execute("""
            INSERT INTO events
              (event_id, store_id, camera_id, visitor_id, event_type,
               timestamp, zone_id, dwell_ms, is_staff, confidence,
               queue_depth, sku_zone, session_seq, ingested_at)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """, (
            event.event_id, event.store_id, event.camera_id, event.visitor_id,
            event.event_type, event.timestamp, event.zone_id, event.dwell_ms,
            int(event.is_staff), event.confidence,
            event.metadata.queue_depth, event.metadata.sku_zone,
            event.metadata.session_seq, ingested_at,
        ))
    except Exception as e:
        if "UNIQUE constraint failed" in str(e):
            raise DuplicateEvent()
        raise


def _latest_ts_for_store(events: list[Event], store_id: str) -> str | None:
    ts_list = [e.timestamp for e in events if e.store_id == store_id]
    return max(ts_list) if ts_list else None
