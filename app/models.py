"""
models.py — Pydantic schemas for events and API responses.
"""
from __future__ import annotations
from pydantic import BaseModel, Field, field_validator
from typing import Optional, Literal
import re

VALID_EVENT_TYPES = Literal[
    "ENTRY", "EXIT", "ZONE_ENTER", "ZONE_EXIT",
    "ZONE_DWELL", "BILLING_QUEUE_JOIN", "BILLING_QUEUE_ABANDON", "REENTRY"
]


class EventMetadata(BaseModel):
    queue_depth: Optional[int] = None
    sku_zone:    Optional[str] = None
    session_seq: int = 0
    group_id:    Optional[str] = None


class Event(BaseModel):
    event_id:   str
    store_id:   str
    camera_id:  str
    visitor_id: str
    event_type: VALID_EVENT_TYPES
    timestamp:  str
    zone_id:    Optional[str] = None
    dwell_ms:   int = 0
    is_staff:   bool = False
    confidence: float = Field(ge=0.0, le=1.0)
    metadata:   EventMetadata = Field(default_factory=EventMetadata)

    @field_validator("event_id")
    @classmethod
    def validate_uuid(cls, v):
        pattern = r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$"
        if not re.match(pattern, v, re.IGNORECASE):
            raise ValueError(f"event_id must be UUID v4: {v}")
        return v

    @field_validator("timestamp")
    @classmethod
    def validate_ts(cls, v):
        from datetime import datetime
        try:
            datetime.strptime(v, "%Y-%m-%dT%H:%M:%SZ")
        except ValueError:
            raise ValueError(f"timestamp must be ISO-8601 UTC: {v}")
        return v


class IngestRequest(BaseModel):
    events: list[Event] = Field(max_length=500)


class IngestResponse(BaseModel):
    accepted: int
    rejected: int
    duplicate: int
    errors: list[dict] = []


# ── Analytics response models ─────────────────────────────────────────────────

class ZoneDwell(BaseModel):
    zone_id:   str
    avg_dwell_ms: float
    visit_count:  int


class StoreMetrics(BaseModel):
    store_id:          str
    unique_visitors:   int
    conversion_rate:   float
    avg_dwell_ms:      float
    queue_depth:       int
    abandonment_rate:  float
    zone_dwells:       list[ZoneDwell]
    window_start:      str
    window_end:        str


class FunnelStage(BaseModel):
    stage: str
    count: int
    drop_off_pct: float


class FunnelResponse(BaseModel):
    store_id: str
    stages:   list[FunnelStage]


class HeatmapZone(BaseModel):
    zone_id:         str
    visit_frequency: float   # 0–100 normalised
    avg_dwell_ms:    float
    data_confidence: bool    # False if < 20 sessions


class HeatmapResponse(BaseModel):
    store_id: str
    zones:    list[HeatmapZone]


class Anomaly(BaseModel):
    anomaly_type:     str
    severity:         Literal["INFO", "WARN", "CRITICAL"]
    description:      str
    suggested_action: str
    detected_at:      str


class AnomalyResponse(BaseModel):
    store_id:  str
    anomalies: list[Anomaly]


class StoreHealth(BaseModel):
    store_id:      str
    status:        Literal["OK", "STALE_FEED", "NO_DATA"]
    last_event_ts: Optional[str]
    lag_minutes:   Optional[float]


class HealthResponse(BaseModel):
    status:  str
    stores:  list[StoreHealth]
    version: str = "0.1.0"
