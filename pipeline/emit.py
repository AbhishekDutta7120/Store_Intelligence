"""
Event schema definition and emission helpers.
All events emitted by the detection pipeline must use emit_event().
"""
import uuid
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

VALID_EVENT_TYPES = {
    "ENTRY", "EXIT", "ZONE_ENTER", "ZONE_EXIT",
    "ZONE_DWELL", "BILLING_QUEUE_JOIN", "BILLING_QUEUE_ABANDON", "REENTRY"
}


def make_event(
    store_id: str,
    camera_id: str,
    visitor_id: str,
    event_type: str,
    timestamp: datetime,
    zone_id: Optional[str] = None,
    dwell_ms: int = 0,
    is_staff: bool = False,
    confidence: float = 1.0,
    queue_depth: Optional[int] = None,
    sku_zone: Optional[str] = None,
    session_seq: int = 0,
    group_id: Optional[str] = None,
) -> dict:
    """Build a fully-validated event dict matching the required schema."""
    assert event_type in VALID_EVENT_TYPES, f"Unknown event type: {event_type}"
    return {
        "event_id": str(uuid.uuid4()),
        "store_id": store_id,
        "camera_id": camera_id,
        "visitor_id": visitor_id,
        "event_type": event_type,
        "timestamp": timestamp.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "zone_id": zone_id,
        "dwell_ms": dwell_ms,
        "is_staff": is_staff,
        "confidence": round(float(confidence), 4),
        "metadata": {
            "queue_depth": queue_depth,
            "sku_zone": sku_zone,
            "session_seq": session_seq,
            "group_id": group_id,
        },
    }


class EventWriter:
    """Writes events to a JSONL file and optionally POSTs to the API."""

    def __init__(self, output_path: str, api_url: Optional[str] = None):
        self.output_path = Path(output_path)
        self.output_path.parent.mkdir(parents=True, exist_ok=True)
        self.api_url = api_url
        self._buffer: list[dict] = []
        self._fh = open(self.output_path, "a", encoding="utf-8")

    def write(self, event: dict):
        line = json.dumps(event, ensure_ascii=False)
        self._fh.write(line + "\n")
        self._fh.flush()
        self._buffer.append(event)
        if len(self._buffer) >= 50:
            self.flush_to_api()

    def flush_to_api(self):
        if not self.api_url or not self._buffer:
            return
        try:
            import urllib.request
            payload = json.dumps({"events": self._buffer}).encode()
            req = urllib.request.Request(
                self.api_url,
                data=payload,
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            urllib.request.urlopen(req, timeout=5)
        except Exception as e:
            print(f"[WARN] API ingest failed: {e}")
        finally:
            self._buffer.clear()

    def close(self):
        self.flush_to_api()
        self._fh.close()
