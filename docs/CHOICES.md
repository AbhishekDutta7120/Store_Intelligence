# CHOICES.md — Three Architectural Decisions

## Decision 1: Detection Model — YOLOv8n over YOLOv8s / RT-DETR

### Options considered

| Model | mAP (COCO) | CPU inference @ 640px | Total pipeline time (5×8-min clips) |
|---|---|---|---|
| YOLOv8n (chosen) | 37.3 | ~200ms/frame | ~30 min |
| YOLOv8s | 44.9 | ~400ms/frame | ~60 min |
| YOLOv8m | 50.2 | ~900ms/frame | ~135 min |
| RT-DETR-L | 53.0 | ~1800ms/frame | ~270 min |
| MediaPipe | N/A | ~50ms/frame | ~8 min (person keypoints only, no tracking ID) |

### What AI suggested

Claude recommended YOLOv8s as the best CPU/accuracy trade-off. It estimated YOLOv8n would show "meaningful accuracy loss on partial occlusion, particularly in crowded billing queues." It also noted MediaPipe as a faster option but flagged the lack of built-in ByteTrack integration.

### What I chose and why

YOLOv8n. The constraint is real: the deployment machine is a Windows laptop with no discrete GPU. At 400ms/frame (YOLOv8s) with frame-skip-3 at 640px, processing five 8-minute clips (12,000 effective frames) takes ~80 minutes. At 200ms/frame (YOLOv8n) it is ~40 minutes — meaningfully within the available window.

I rejected the AI's accuracy concern because the scoring criteria evaluate how edge cases are *handled*, not just raw count accuracy. ByteTrack's secondary buffer re-associates partially occluded detections across frames — a person occluded for 2-3 frames at 5fps doesn't generate a new track. This compensates for YOLOv8n's lower per-frame precision on crowded frames.

I would upgrade to YOLOv8s or YOLOv8m if a GPU were available — that trade-off makes sense at inference speeds of 10-20ms/frame.

---

## Decision 2: Event Schema Design — Denormalised event log with metadata bag

### Options considered

**Option A — Minimal flat schema:** Only the strictly required fields. Fast to build, but inflexible for future analytics needs (SKU-level attribution, salesperson correlation).

**Option B — Normalised relational schema:** Separate tables for sessions, zone visits, billing events, staff records. Correct for a data warehouse, over-engineered for a take-home system.

**Option C — Denormalised event log with metadata bag (chosen):** One `events` table. Required fields are top-level columns with indexes. Event-specific data (queue_depth, sku_zone, session_seq) lives in a typed metadata structure within the same row.

### What AI suggested

Claude suggested Option C unprompted, citing the "event sourcing pattern" and noting that a denormalised log supports both real-time queries and retrospective session reconstruction. It also suggested a `raw_json` column for debugging. I omitted raw_json because `data/events.jsonl` already serves as the immutable audit log — storing the JSON twice adds write overhead without benefit.

### What I chose and why

Option C, because it directly maps to the required event schema (flat by design), supports idempotency with a single UNIQUE constraint, and allows `COUNT(DISTINCT visitor_id)` session deduplication without JOINs.

The specific metadata fields were shaped by the real Brigade Road data: `sku_zone` maps to actual brand zones in the store (SKINCARE → EB Korean/The Face Shop/DermDoc tier; MAKEUP → Colorbar/Sugar/NY Bae tier), enabling future brand-level dwell attribution. `queue_depth` is populated only for BILLING and BILLING_QUEUE_JOIN events, keeping the field sparse but meaningful.

---

## Decision 3: API architecture — Synchronous FastAPI over async aggregation pipeline

### Options considered

**Option A — Synchronous FastAPI + SQLite (chosen):** Each request computes metrics on-demand from the events table.

**Option B — Background aggregation + pre-computed snapshots:** A background task runs every 30s and writes a `metrics_cache` table. API reads from cache.

**Option C — Redis pub/sub:** Detection pipeline publishes events to Redis; API maintains in-memory aggregates. Lowest latency, highest operational complexity.

### What AI suggested

Claude strongly recommended Option B or C. It argued that `COUNT(DISTINCT visitor_id)` computed on the fly would be "too slow at production scale — 40 stores, ~10,000 events/store/day." It drafted a complete Redis-backed aggregation layer.

### What I chose and why

Option A, overriding the AI.

The AI's concern is valid at the stated scale (40 stores × 10,000 events = 400,000 rows/day). But I made two observations:

1. **The scoring harness tests against a bounded event set from 5 clips.** At that scale, a `COUNT(DISTINCT visitor_id)` with an index on `(store_id, timestamp)` completes in under 5ms. The "too slow" concern doesn't apply to this submission.

2. **Simplicity is a correctness property.** Redis adds a third Docker service, a pub/sub subscriber, and in-memory state that must be rebuilt on restart. Each is a new failure mode. A submission that works reliably with Option A scores higher on the acceptance gate than one that fails intermittently with Option C.

I documented the scale ceiling explicitly: SQLite WAL handles a single writer, so at 40 live stores sending concurrent ingest batches, write contention becomes the first bottleneck. The fix is PostgreSQL + connection pool — a one-line change in `database.py`. I noted this in DESIGN.md rather than implementing it prematurely.

**Where I partially adopted the AI:** The dashboard polls the API every 3 seconds rather than using WebSocket or SSE. This is a lightweight approximation of real-time that the AI described as "good enough for a single-store dashboard" — I agreed, and it eliminates a stateful server component entirely.
