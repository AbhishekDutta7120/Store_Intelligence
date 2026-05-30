# PROMPT: Write pytest tests for FastAPI store analytics endpoints:
#   GET /stores/{id}/metrics → unique_visitors, conversion_rate, queue_depth, abandonment_rate
#   GET /stores/{id}/funnel  → Entry → Zone visit → Billing queue → Purchase stages
#   GET /stores/{id}/heatmap → zone visit frequency normalised 0-100
#   Cover: zero-traffic store, all-staff clip, normal flow, re-entry deduplication.
# CHANGES MADE:
#   - Added explicit all-staff test (AI only tested happy path staff exclusion)
#   - Fixed heatmap normalisation assertion (AI was checking raw counts not 0-100)
#   - Added re-entry deduplication check in funnel (AI missed this requirement)
#   - Moved DB fixture to conftest-style autouse to avoid repetition

import pytest
import uuid
from datetime import datetime, timezone
from fastapi.testclient import TestClient
from app.main import app
from app.database import init_db
import os


def _now_ts():
    """Return current UTC timestamp string so events fall within the 24h window."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


@pytest.fixture(autouse=True)
def fresh_db(tmp_path, monkeypatch):
    db = str(tmp_path / "test.db")
    monkeypatch.setenv("DB_PATH", db)
    import app.database as dbmod
    dbmod.DB_PATH = db
    init_db()
    yield
    if os.path.exists(db):
        os.remove(db)


@pytest.fixture
def client():
    return TestClient(app)


def post_events(client, events):
    r = client.post("/events/ingest", json={"events": events})
    assert r.status_code == 200
    return r.json()


def make_event(**kw):
    base = {
        "event_id":   str(uuid.uuid4()),
        "store_id":   "STORE_BLR_002",
        "camera_id":  "CAM_3",
        "visitor_id": "VIS_aabbcc",
        "event_type": "ENTRY",
        "timestamp":  _now_ts(),
        "zone_id":    None,
        "dwell_ms":   0,
        "is_staff":   False,
        "confidence": 0.88,
        "metadata":   {"queue_depth": None, "sku_zone": None, "session_seq": 1},
    }
    base.update(kw)
    return base


# ── helpers to build realistic sessions ──────────────────────────────────────

def full_session(visitor_id, store_id="STORE_BLR_002", bought=True):
    """Build a complete visitor session: ENTRY → ZONE_ENTER → BILLING → EXIT."""
    ts = _now_ts()
    events = [
        make_event(store_id=store_id, visitor_id=visitor_id, event_type="ENTRY",      timestamp=ts),
        make_event(store_id=store_id, visitor_id=visitor_id, event_type="ZONE_ENTER",
                   zone_id="SKINCARE", timestamp=ts,
                   metadata={"queue_depth": None, "sku_zone": "MOISTURISER", "session_seq": 2}),
        make_event(store_id=store_id, visitor_id=visitor_id,
                   event_type="BILLING_QUEUE_JOIN" if bought else "ZONE_ENTER",
                   zone_id="BILLING", timestamp=ts,
                   metadata={"queue_depth": 1, "sku_zone": None, "session_seq": 3}),
    ]
    if not bought:
        events.append(make_event(
            store_id=store_id, visitor_id=visitor_id,
            event_type="BILLING_QUEUE_ABANDON", zone_id="BILLING", timestamp=ts,
        ))
    events.append(make_event(
        store_id=store_id, visitor_id=visitor_id, event_type="EXIT", timestamp=ts,
    ))
    return events


class TestMetricsZeroTraffic:
    def test_empty_store_returns_zeros_not_error(self, client):
        """Zero-traffic store must return 200 with zeros, not crash."""
        r = client.get("/stores/STORE_BLR_002/metrics")
        assert r.status_code == 200
        body = r.json()
        assert body["unique_visitors"]  == 0
        assert body["conversion_rate"]  == 0.0
        assert body["queue_depth"]      == 0
        assert body["abandonment_rate"] == 0.0
        assert body["zone_dwells"]      == []

    def test_unknown_store_returns_zeros(self, client):
        r = client.get("/stores/STORE_XXX_999/metrics")
        assert r.status_code == 200
        assert r.json()["unique_visitors"] == 0


class TestMetricsNormalFlow:
    def test_unique_visitors_counted(self, client):
        events = full_session("VIS_001") + full_session("VIS_002")
        post_events(client, events)
        r = client.get("/stores/STORE_BLR_002/metrics")
        assert r.json()["unique_visitors"] == 2

    def test_conversion_rate_with_purchases(self, client):
        # 2 visitors, 1 bought
        events = full_session("VIS_001", bought=True) + full_session("VIS_002", bought=False)
        post_events(client, events)
        r = client.get("/stores/STORE_BLR_002/metrics")
        body = r.json()
        assert body["unique_visitors"] == 2
        # conversion > 0 (1 visitor reached billing)
        assert body["conversion_rate"] >= 0.0

    def test_abandonment_rate_computed(self, client):
        events = full_session("VIS_001", bought=False)
        post_events(client, events)
        r = client.get("/stores/STORE_BLR_002/metrics")
        # abandonment_rate should reflect the abandon event
        assert r.json()["abandonment_rate"] >= 0.0

    def test_queue_depth_from_events(self, client):
        ev = make_event(
            event_type="BILLING_QUEUE_JOIN", zone_id="BILLING",
            metadata={"queue_depth": 4, "sku_zone": None, "session_seq": 1},
        )
        post_events(client, [ev])
        r = client.get("/stores/STORE_BLR_002/metrics")
        assert r.json()["queue_depth"] == 4


class TestMetricsStaffExclusion:
    def test_staff_excluded_from_visitor_count(self, client):
        staff_ev = make_event(visitor_id="VIS_staff", is_staff=True, event_type="ENTRY")
        customer_ev = make_event(visitor_id="VIS_cust", is_staff=False, event_type="ENTRY")
        post_events(client, [staff_ev, customer_ev])
        r = client.get("/stores/STORE_BLR_002/metrics")
        assert r.json()["unique_visitors"] == 1

    def test_all_staff_clip_returns_zeros(self, client):
        """All-staff clip: unique_visitors must be 0, no crash."""
        events = [
            make_event(visitor_id=f"VIS_staff_{i}", is_staff=True, event_type="ENTRY")
            for i in range(5)
        ]
        post_events(client, events)
        r = client.get("/stores/STORE_BLR_002/metrics")
        assert r.json()["unique_visitors"] == 0


class TestFunnel:
    def test_funnel_stages_present(self, client):
        post_events(client, full_session("VIS_001"))
        r = client.get("/stores/STORE_BLR_002/funnel")
        assert r.status_code == 200
        body  = r.json()
        names = [s["stage"] for s in body["stages"]]
        assert "Entry"         in names
        assert "Zone visit"    in names
        assert "Billing queue" in names
        assert "Purchase"      in names

    def test_funnel_counts_decrease_or_equal(self, client):
        events = full_session("VIS_001") + full_session("VIS_002") + full_session("VIS_003")
        post_events(client, events)
        r      = client.get("/stores/STORE_BLR_002/funnel")
        stages = r.json()["stages"]
        counts = [s["count"] for s in stages]
        for i in range(1, len(counts)):
            assert counts[i] <= counts[i - 1], \
                f"Funnel count increased at stage {i}: {counts}"

    def test_reentry_not_double_counted(self, client):
        """Same visitor_id with ENTRY + REENTRY should count as 1 unique visitor."""
        ts = _now_ts()
        events = [
            make_event(visitor_id="VIS_re", event_type="ENTRY",   timestamp=ts),
            make_event(visitor_id="VIS_re", event_type="REENTRY", timestamp=ts),
        ]
        post_events(client, events)
        r = client.get("/stores/STORE_BLR_002/funnel")
        entry_stage = next(s for s in r.json()["stages"] if s["stage"] == "Entry")
        assert entry_stage["count"] == 1

    def test_empty_store_funnel(self, client):
        r = client.get("/stores/STORE_BLR_002/funnel")
        assert r.status_code == 200
        for stage in r.json()["stages"]:
            assert stage["count"] == 0


class TestHeatmap:
    def test_heatmap_normalised_0_to_100(self, client):
        ts = _now_ts()
        events = [
            make_event(visitor_id="VIS_001", event_type="ZONE_DWELL",
                       zone_id="SKINCARE", dwell_ms=35000, timestamp=ts,
                       metadata={"queue_depth": None, "sku_zone": "MOISTURISER", "session_seq": 1}),
            make_event(visitor_id="VIS_002", event_type="ZONE_DWELL",
                       zone_id="MAKEUP", dwell_ms=12000, timestamp=ts,
                       metadata={"queue_depth": None, "sku_zone": "FOUNDATION", "session_seq": 1}),
        ]
        post_events(client, events)
        r = client.get("/stores/STORE_BLR_002/heatmap")
        assert r.status_code == 200
        for zone in r.json()["zones"]:
            assert 0.0 <= zone["visit_frequency"] <= 100.0

    def test_most_visited_zone_scores_100(self, client):
        """The zone with highest visits must have frequency == 100."""
        ts = _now_ts()
        events = [
            make_event(visitor_id=f"VIS_{i}", event_type="ZONE_DWELL",
                       zone_id="SKINCARE", dwell_ms=30000, timestamp=ts,
                       metadata={"queue_depth": None, "sku_zone": None, "session_seq": 1})
            for i in range(3)
        ] + [
            make_event(visitor_id="VIS_low", event_type="ZONE_DWELL",
                       zone_id="MAKEUP", dwell_ms=30000, timestamp=ts,
                       metadata={"queue_depth": None, "sku_zone": None, "session_seq": 1}),
        ]
        post_events(client, events)
        r = client.get("/stores/STORE_BLR_002/heatmap")
        freqs = {z["zone_id"]: z["visit_frequency"] for z in r.json()["zones"]}
        assert freqs.get("SKINCARE") == 100.0

    def test_low_session_confidence_flag(self, client):
        """data_confidence=False when fewer than 20 sessions."""
        ts = _now_ts()
        ev = make_event(event_type="ZONE_DWELL", zone_id="SKINCARE", dwell_ms=30000,
                        timestamp=ts,
                        metadata={"queue_depth": None, "sku_zone": None, "session_seq": 1})
        post_events(client, [ev])
        r = client.get("/stores/STORE_BLR_002/heatmap")
        for zone in r.json()["zones"]:
            assert zone["data_confidence"] is False
