# Store Intelligence — Apex Retail

Real-time offline store analytics from raw CCTV footage.

## Setup in 5 Commands

```bash
# 1. Clone and enter the repo
git clone <https://github.com/AbhishekDutta7120/Store_Intelligence> store-intelligence && cd store-intelligence

# 2. Install Python dependencies (detection pipeline)
pip install -r requirements.txt

# 3. Start the API
docker compose up --build -d

# 4. Run the detection pipeline on your video clips
python -m pipeline.detect --videos "CAM 1.mp4" "CAM 2.mp4" "CAM 3.mp4" "CAM 4.mp4" "CAM 5.mp4" --layout config/store_layout.json --output data/events.jsonl --api-url http://localhost:8000/events/ingest

# 5. Open the live dashboard
https://store-intelligence-jsyb.onrender.com/
```

> **Windows users:** Use `pipeline\run.bat <path-to-videos>` for a one-command pipeline run.

---

## What's Running

| Service | URL | Description |
|---|---|---|
| API | http://localhost:8000 | FastAPI + SQLite |
| Dashboard | http://localhost:8000 | Live metrics dashboard |
| API Docs | http://localhost:8000/docs | Swagger UI |

---

## API Endpoints

| Method | Endpoint | Description |
|---|---|---|
| POST | `/events/ingest` | Ingest up to 500 events (idempotent) |
| GET | `/stores/{id}/metrics` | Visitors, conversion rate, queue depth |
| GET | `/stores/{id}/funnel` | Entry → Zone → Billing → Purchase |
| GET | `/stores/{id}/heatmap` | Zone visit frequency (0–100) |
| GET | `/stores/{id}/anomalies` | Queue spikes, conversion drops, dead zones |
| GET | `/health` | Service status + STALE_FEED detection |

**Example:**
```bash
curl http://localhost:8000/stores/ST1008/metrics
```

---

## Running the Detection Pipeline

The detection pipeline processes your CCTV clips locally (not inside Docker) and POSTs events to the API.

### Requirements
- Python 3.10+
- `pip install ultralytics opencv-python-headless numpy`
- YOLOv8n model (auto-downloaded on first run)

### Command
```bash
python -m pipeline.detect \
  --videos "CAM 1.mp4" "CAM 2.mp4" "CAM 3.mp4" "CAM 4.mp4" "CAM 5.mp4" \
  --layout config/store_layout.json \
  --output data/events.jsonl \
  --api-url http://localhost:8000/events/ingest
```

**Flags:**
- `--videos` — paths to your video files (space-separated)
- `--layout` — path to store_layout.json
- `--output` — where to write the events JSONL file
- `--api-url` — if set, events are POSTed live to the API (optional)

**CPU processing time:** ~20–40 minutes for 5 × 8-minute clips on a modern CPU.

### Output
Events are written to `data/events.jsonl` — one JSON object per line. If `--api-url` is set, events are also POSTed in batches of 50 to the API as they are generated.

---

## Running Tests

```bash
# Install test dependencies
pip install pytest pytest-cov httpx

# Run all tests with coverage
pytest tests/ -v --cov=app --cov-report=term-missing
```

Expected output: >70% statement coverage.

---

## Project Structure

```
store-intelligence/
├── pipeline/
│   ├── detect.py       # Main detection + tracking script
│   ├── tracker.py      # Re-ID and visitor state machine
│   ├── emit.py         # Event schema and file/API writer
│   └── run.bat         # Windows one-command runner
├── app/
│   ├── main.py         # FastAPI app + logging middleware
│   ├── models.py       # Pydantic event schema
│   ├── database.py     # SQLite setup
│   ├── ingestion.py    # POST /events/ingest
│   ├── metrics.py      # GET /stores/{id}/metrics
│   ├── funnel.py       # GET /stores/{id}/funnel + /heatmap
│   ├── anomalies.py    # GET /stores/{id}/anomalies
│   └── health.py       # GET /health
├── dashboard/
│   └── index.html      # Live web dashboard (polls every 3s)
├── tests/
│   ├── test_ingestion.py
│   ├── test_metrics.py
│   └── test_anomalies.py
├── config/
│   └── store_layout.json
├── docs/
│   ├── DESIGN.md
│   └── CHOICES.md
├── Dockerfile
├── docker-compose.yml
└── README.md
```

---

## Live Dashboard (Part E)

Live dashboard link:- https://store-intelligence-jsyb.onrender.com/

The dashboard shows:
- Unique visitor count (live)
- Conversion rate
- Queue depth at billing
- Abandonment rate
- Conversion funnel with drop-off percentages
- Zone heatmap (visit frequency + average dwell)
- Active anomalies with severity and suggested actions

It refreshes automatically every 3 seconds as events flow in from the detection pipeline.

---

## Camera Mapping

| File | Camera ID | Type | Zone |
|---|---|---|---|
| CAM 1.mp4 | CAM_1 | Floor | Skincare shelf — FarmStay, The Face Shop, Good Vibes, DermaCo |
| CAM 2.mp4 | CAM_2 | Floor | Makeup + Brands — Alps, Swiss Beauty, Lakme, Faces Canada, Maybelline |
| CAM 3.mp4 | CAM_3 | Entry/Exit | Main entrance/exit threshold |
| CAM 4.mp4 | CAM_4 | Stockroom | Back room — excluded from customer metrics |
| CAM 5.mp4 | CAM_5 | Billing | Cash counter |

---

## Notes

- The API handles zero-traffic gracefully — `/metrics` returns zeros, never 404 or null.
- `POST /events/ingest` is fully idempotent — safe to call twice with the same payload.
- Staff events (`is_staff=true`) are excluded from all customer-facing metrics.
- Re-entries reuse the same `visitor_id`, so a customer counted twice is never double-counted in the funnel.
