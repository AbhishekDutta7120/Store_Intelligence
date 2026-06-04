# DESIGN.md — Store Intelligence System

## Overview

This system converts raw CCTV footage from Apex Retail's Brigade Road, Bangalore store (ST1008) into a live analytics API. It is built in two independent stages: an offline detection pipeline that processes video files and emits structured events, and a containerised REST API that ingests those events and serves real-time store intelligence.

The North Star metric is **offline store conversion rate** — the fraction of unique visitors who completed a purchase. Every component either improves the accuracy of this number (detection layer) or makes it actionable (API layer).

The system was built with real transaction data from April 10, 2026 (24 aggregated orders, ₹198–₹8,243 basket values) and the actual Brigade Road store floor plan, which defines 9 named zones mapped to 5 camera angles.

---

## System Architecture

```
CCTV Videos (CAM_1..5)       Brigade Road, Bangalore (ST1008)
        │
        ▼
┌─────────────────────────────┐
│  Detection Pipeline         │  Runs locally (CPU, Windows)
│  detect.py + tracker.py     │
│  YOLOv8n · ByteTrack        │  ~25-35 min for 5×8-min clips
│  Re-ID via HSV histogram    │
│  Staff heuristic (65% clip) │
└────────────┬────────────────┘
             │ POST /events/ingest (batches of 50)
             │ + writes data/events.jsonl
             ▼
┌─────────────────────────────┐
│  FastAPI (Docker)           │  Port 8000
│  SQLite WAL mode            │
│  ├─ POST /events/ingest     │  Idempotent by event_id
│  ├─ GET  /stores/{id}/metrics│  24h rolling window
│  ├─ GET  /stores/{id}/funnel │  Session-level dedup
│  ├─ GET  /stores/{id}/heatmap│  0-100 normalised
│  ├─ GET  /stores/{id}/anomalies│ Queue spike, conversion drop
│  └─ GET  /health            │  STALE_FEED if >10 min lag
└────────────┬────────────────┘
             │ polls every 3s
             ▼
┌─────────────────────────────┐
│  Live Dashboard (HTML/JS)   │  Served at https://store-intelligence-jsyb.onrender.com/
└─────────────────────────────┘
```

---

## Real Store: Brigade Road, Bangalore (ST1008)

The store layout (Brigade_Road_Store_layout.xlsx) defines a single-floor beauty retail space with the following zone structure:

| Zone | Camera | Description |
|---|---|---|
| ENTRY | CAM_3 | Main entrance/exit — glass door threshold |
| SKINCARE | CAM_1 | Skincare shelf — FarmStay, The Face Shop, Good Vibes, DermaCo, Minimalist, Aqualogica |
| PMU | CAM_1 | Permanent Makeup Unit — vanity station right side |
| MAKEUP | CAM_2 | Makeup brands — Swiss Beauty, Lakme, Faces Canada, Maybelline |
| HAIRCARE | CAM_2 | Hair care — Alps, L'Oreal |
| ACCESSORIES | CAM_2 | Accessories — top left of makeup floor |
| BILLING | CAM_5 | Cash counter — billing area |
| STOCKROOM | CAM_4 | Back room — excluded from customer metrics |

---
## Note on Event Schema

The events.jsonl files follow the schema defined in the official problem 
statement PDF. The sample_events.jsonl provided uses an alternative schema 
with different field names (store_code vs store_id, id_token vs visitor_id, 
event_timestamp vs timestamp). We followed the PDF schema as it is the 
authoritative specification for this challenge.

The generated event files are:
- data/events_store1.jsonl — ST1008 Brigade Road, Bangalore (331 events)
- data/events_store2.jsonl — store_1076 (events from second store)

## Stage 1: Detection Pipeline

### Model: YOLOv8n (CPU-optimised)

YOLOv8n was chosen because the deployment target is a Windows laptop with no dedicated GPU. At 1080p with frame-skip-3 and 640px resize, it processes all five 8-minute clips in ~25-35 minutes. Heavier models (YOLOv8m, RT-DETR) would take 4-8 hours on the same hardware, making them impractical.

### Frame skipping and resize

Every 3rd frame is processed (effectively 5 fps from 15 fps source). The frame is resized to 640px longest side before inference. Detections are scaled back to original coordinates before zone assignment. At 5 fps, a person walking at 1 m/s across a 3-metre doorway produces ~15 sampled frames — sufficient for accurate crossing detection.

### Tracking: ByteTrack

ByteTrack is included in ultralytics with no additional install. Unlike SORT, it maintains a secondary buffer for low-confidence detections, which handles partial occlusion (a known challenge in the billing queue area) without dropping tracks.

### Re-ID: HSV colour histogram

Each bounding box crop is converted to an 8×8 HSV histogram (64 values). When a new track appears, cosine similarity is computed against all tracks that exited within the past 120 seconds. Similarity ≥ 0.75 → REENTRY event, same visitor_id reused.

This avoids heavy Re-ID models (OSNet, torchreid) that require CUDA. The trade-off — two customers in similar clothing could be conflated — is acceptable in a beauty retail context where customer clothing is highly varied.

### Staff detection

After each clip, any track continuously visible for >65% of clip duration is flagged is_staff=true. This catches staff members who are present throughout their shift while reliably excluding customers (typical dwell: 5–20 minutes on an 8-minute clip).
*Fix update:* The pipeline buffers events during clip processing and updates the `is_staff` flag accurately *after* calculating durations, ensuring staff entries are correctly marked before flushing to the API.

### Entry/exit counting

A virtual horizontal line at 55% frame height acts as the entry/exit threshold for CAM_1. Centroid crossing the line downward → ENTRY; upward → EXIT. Direction is computed from the delta between previous and current normalised centroid y-position.

### POS correlation

The real POS data (24 orders, April 10 2026, basket values ₹198–₹8,243) is loaded from `data/pos_transactions.csv`. A visitor who was in the BILLING zone within 5 minutes before a transaction timestamp is counted as a converted customer for that session.

---

## Stage 2: Intelligence API

### Storage: SQLite with WAL mode

WAL (Write-Ahead Logging) mode supports concurrent reads with a single writer. For the event volumes expected (thousands of events per store per day), this is sufficient without introducing PostgreSQL or Redis.

Three indexes cover the three main access patterns:
- `(store_id, timestamp)` — time-window queries in /metrics, /funnel, /heatmap
- `(visitor_id, store_id)` — session reconstruction
- `(event_type, store_id)` — anomaly detection filters

### Idempotency

`POST /events/ingest` uses a UNIQUE constraint on `event_id`. The same batch sent twice returns `{"accepted": 0, "duplicate": N}` without errors. Safe for retry loops from the detection pipeline.

### Session deduplication

All funnel and metrics endpoints use `COUNT(DISTINCT visitor_id)`. Since the Re-ID system reuses visitor_id on re-entry, a customer who leaves and returns is counted once — not twice.

### Observability
Structured JSON logs are emitted for every request including trace_id, store_id, endpoint, latency_ms, and status_code — sufficient for production debugging and monitoring.

### Anomaly detection

Four anomaly types are implemented:
- `BILLING_QUEUE_SPIKE`: max queue_depth in last hour > 5 (WARN) or > 8 (CRITICAL)
- `CONVERSION_DROP`: today's rate > 30% below 7-day average
- `DEAD_ZONE`: zone with historical visits shows none in last 30 minutes
- `NO_TRAFFIC`: store has historical events but none in last 30 minutes

---

## AI-Assisted Decisions

### 1. Re-ID approach — adopted AI suggestion with hardware-aware modification

Claude suggested OSNet (torchreid) as the most accurate Re-ID approach, then proposed HSV histogram cosine similarity as a CPU-friendly fallback. I adopted the fallback but changed the colour space from L\*a\*b\* (AI's suggestion) to HSV, because HSV separates hue and saturation more intuitively and is more robust to the mixed lighting conditions described in the problem brief (natural light, fluorescent, mixed). I validated this choice by reasoning through the Brigade Road store layout — a mix of spot lighting over product shelves and ambient ceiling lighting — where L\*a\*b\* distances would be less stable across zones.

### 2. Zone definitions — derived from real floor plan, not AI-generated

The AI initially suggested generic retail zones (SKINCARE, MAKEUP, HAIRCARE, FRAGRANCE). After receiving the actual Brigade Road store layout image, I redefined zones to match the real floor plan: SKINCARE (top shelf only), MAKEUP (central unit + bottom brands), FOH (open floor), FRAGRANCE (left center with nail unit), BILLING (cash counter right), PMU (bottom right corner). The AI's generic zones would have been inaccurate for this specific store.

### 3. Database choice — overrode AI suggestion of PostgreSQL + Redis

The AI recommended PostgreSQL for write durability and Redis for caching real-time metrics. I chose SQLite because: (a) single-service Docker Compose is simpler to operate; (b) WAL mode handles concurrent reads adequately; (c) `COUNT(DISTINCT visitor_id)` over thousands of rows completes in <10ms with proper indexes. I documented the scale ceiling (write contention at ~40 simultaneous stores) in CHOICES.md.
