# PROMPT: Write pytest tests for store anomaly detection and health endpoints.
#   Anomaly types: BILLING_QUEUE_SPIKE, CONVERSION_DROP, DEAD_ZONE, NO_TRAFFIC.
#   Health: GET /health returns STALE_FEED when last event > 10 min ago.
#   Cover: severity levels, no anomalies case, stale feed detection, empty store health.
# CHANGES MADE:
#   - Added DEAD_ZONE test (AI only covered queue and conversion anomalies)
#   - Fixed timestamp arithmetic for STALE_FEED test (AI used naive datetime)
#   - Added suggested_action field check (AI forgot this required field)
#   - Added test for store with zero events in health log

import pytest
import uuid
from datetime import datetime, timezone, timedelta
from fastapi.testclient import TestClient
from app.main import app
from app.database import init_db
import os


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


def make_event(**kw):
    base = {
        "event_id":   str(uuid.uuid4()),
        "store_id":   "STORE_BLR_002",
        "camera_id":  "CAM_5",
        "visitor_id": "VIS_test",
        "event_type": "ENTRY",
        "timestamp":  datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "zone_id":    None,
        "dwell_ms":   0,
        "is_staff":   False,
        "confidence": 0.9,
        "metadata":   {"queue_depth": None, "sku_zone": None, "session_seq": 1},
    }
    base.update(kw)
    return base


class TestNoAnomalies:
    def test_empty_store_no_anomalies_crash(self, client):
        r = client.get("/stores/STORE_BLR_002/anomalies")
        assert r.status_code == 200
        body = r.json()
        assert "anomalies" in body
        assert isinstance(body["anomalies"], list)

    def test_normal_traffic_no_spike(self, client):
        events = [
            make_event(visitor_id=f"VIS_{i}", event_type="BILLING_QUEUE_JOIN",
                       zone_id="BILLING",
                       metadata={"queue_depth": 2, "sku_zone": None, "session_seq": 1})
            for i in range(3)
        ]
        post_events(client, events)
        r = client.get("/stores/STORE_BLR_002/anomalies")
        types = [a["anomaly_type"] for a in r.json()["anomalies"]]
        assert "BILLING_QUEUE_SPIKE" not in types


class TestQueueSpike:
    def test_queue_spike_critical_above_8(self, client):
        ev = make_event(
            event_type="BILLING_QUEUE_JOIN", zone_id="BILLING",
            metadata={"queue_depth": 10, "sku_zone": None, "session_seq": 1},
        )
        post_events(client, [ev])
        r = client.get("/stores/STORE_BLR_002/anomalies")
        spikes = [a for a in r.json()["anomalies"] if a["anomaly_type"] == "BILLING_QUEUE_SPIKE"]
        assert len(spikes) >= 1
        assert spikes[0]["severity"] == "CRITICAL"

    def test_queue_spike_warn_between_5_and_8(self, client):
        ev = make_event(
            event_type="BILLING_QUEUE_JOIN", zone_id="BILLING",
            metadata={"queue_depth": 6, "sku_zone": None, "session_seq": 1},
        )
        post_events(client, [ev])
        r = client.get("/stores/STORE_BLR_002/anomalies")
        spikes = [a for a in r.json()["anomalies"] if a["anomaly_type"] == "BILLING_QUEUE_SPIKE"]
        assert len(spikes) >= 1
        assert spikes[0]["severity"] == "WARN"

    def test_anomaly_has_suggested_action(self, client):
        ev = make_event(
            event_type="BILLING_QUEUE_JOIN", zone_id="BILLING",
            metadata={"queue_depth": 9, "sku_zone": None, "session_seq": 1},
        )
        post_events(client, [ev])
        r = client.get("/stores/STORE_BLR_002/anomalies")
        for a in r.json()["anomalies"]:
            assert "suggested_action" in a
            assert len(a["suggested_action"]) > 5


class TestDeadZone:
    def test_dead_zone_detected_for_inactive_zone(self, client):
        """Zone that had visits in past but not last 30 min → DEAD_ZONE."""
        # Use an old timestamp (> 30 min ago) so zone appears dead
        old_ts = (datetime.now(timezone.utc) - timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
        ev = make_event(
            event_type="ZONE_ENTER", zone_id="SKINCARE", timestamp=old_ts,
            metadata={"queue_depth": None, "sku_zone": "MOISTURISER", "session_seq": 1},
        )
        post_events(client, [ev])
        r = client.get("/stores/STORE_BLR_002/anomalies")
        dead = [a for a in r.json()["anomalies"] if a["anomaly_type"] == "DEAD_ZONE"]
        assert len(dead) >= 1
        assert dead[0]["severity"] == "INFO"


class TestNoTraffic:
    def test_no_traffic_anomaly_after_quiet_period(self, client):
        """Store that had events long ago but nothing recent → NO_TRAFFIC."""
        old_ts = (datetime.now(timezone.utc) - timedelta(hours=2)).strftime("%Y-%m-%dT%H:%M:%SZ")
        ev = make_event(timestamp=old_ts)
        post_events(client, [ev])
        r = client.get("/stores/STORE_BLR_002/anomalies")
        no_traffic = [a for a in r.json()["anomalies"] if a["anomaly_type"] == "NO_TRAFFIC"]
        assert len(no_traffic) >= 1


class TestHealth:
    def test_health_ok_with_no_stores(self, client):
        r = client.get("/health")
        assert r.status_code == 200
        body = r.json()
        assert body["status"] == "OK"
        assert "stores" in body
        assert "version" in body

    def test_health_ok_after_fresh_events(self, client):
        now_ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        post_events(client, [make_event(timestamp=now_ts)])
        r = client.get("/health")
        assert r.status_code == 200
        stores = {s["store_id"]: s for s in r.json()["stores"]}
        assert stores["STORE_BLR_002"]["status"] == "OK"

    def test_stale_feed_detected(self, client):
        """Events from >10 min ago must trigger STALE_FEED status."""
        stale_ts = (datetime.now(timezone.utc) - timedelta(minutes=15)).strftime("%Y-%m-%dT%H:%M:%SZ")
        post_events(client, [make_event(timestamp=stale_ts)])
        r = client.get("/health")
        stores = {s["store_id"]: s for s in r.json()["stores"]}
        assert stores["STORE_BLR_002"]["status"] == "STALE_FEED"
        assert stores["STORE_BLR_002"]["lag_minutes"] > 10

    def test_health_includes_last_event_ts(self, client):
        ts = "2026-03-03T14:00:00Z"
        post_events(client, [make_event(timestamp=ts)])
        r = client.get("/health")
        stores = {s["store_id"]: s for s in r.json()["stores"]}
        assert stores["STORE_BLR_002"]["last_event_ts"] == ts
