"""
tracker.py — Per-visitor state machine + Re-ID logic.

Re-ID strategy (CPU-friendly):
  - When a track disappears, store its last bounding box crop histogram.
  - When a new track appears, compare colour histograms of all recently-exited tracks.
  - If cosine similarity > RE_ID_THRESHOLD and time gap < RE_ID_WINDOW_SEC, it's a re-entry.

Staff detection heuristic:
  - A track present for > STAFF_DWELL_RATIO of the clip duration is flagged as staff.
  - Updated lazily when processing ends.
"""
import cv2
import numpy as np
from collections import defaultdict
from datetime import datetime, timedelta
from typing import Optional
import hashlib

# ── tuneable constants ──────────────────────────────────────────────────────
RE_ID_THRESHOLD   = 0.75   # cosine similarity for Re-ID match
RE_ID_WINDOW_SEC  = 120    # max seconds between exit and re-entry to attempt match
STAFF_DWELL_RATIO = 0.65   # if tracked > 65% of clip → staff
DWELL_EMIT_INTERVAL = 30   # seconds between ZONE_DWELL emissions


def _colour_histogram(frame: np.ndarray, box: list[float]) -> np.ndarray:
    """Extract normalised HSV colour histogram from a bounding box crop."""
    h, w = frame.shape[:2]
    x1 = max(0, int(box[0]))
    y1 = max(0, int(box[1]))
    x2 = min(w, int(box[2]))
    y2 = min(h, int(box[3]))
    if x2 <= x1 or y2 <= y1:
        return np.zeros(64, dtype=np.float32)
    crop = frame[y1:y2, x1:x2]
    hsv  = cv2.cvtColor(crop, cv2.COLOR_BGR2HSV)
    hist = cv2.calcHist([hsv], [0, 1], None, [8, 8], [0, 180, 0, 256])
    cv2.normalize(hist, hist)
    return hist.flatten()


def _cosine_sim(a: np.ndarray, b: np.ndarray) -> float:
    if a is None or b is None or len(a) == 0 or len(b) == 0:
        return 0.0
    denom = (np.linalg.norm(a) * np.linalg.norm(b))
    return float(np.dot(a, b) / denom) if denom > 1e-9 else 0.0


def _make_visitor_id(seed: str) -> str:
    return "VIS_" + hashlib.md5(seed.encode()).hexdigest()[:6]


class VisitorTrack:
    __slots__ = (
        "visitor_id", "track_id", "store_id", "camera_id",
        "entry_time", "last_seen", "current_zone", "zone_entry_time",
        "last_dwell_emit", "is_staff", "confidence", "histogram",
        "session_seq", "exited", "exit_time", "in_billing",
    )

    def __init__(self, track_id: int, visitor_id: str, store_id: str,
                 camera_id: str, entry_time: datetime, confidence: float,
                 histogram: Optional[np.ndarray] = None):
        self.visitor_id     = visitor_id
        self.track_id       = track_id
        self.store_id       = store_id
        self.camera_id      = camera_id
        self.entry_time     = entry_time
        self.last_seen      = entry_time
        self.current_zone   = None
        self.zone_entry_time= None
        self.last_dwell_emit= None
        self.is_staff       = False
        self.confidence     = confidence
        self.histogram      = histogram
        self.session_seq    = 0
        self.exited         = False
        self.exit_time      = None
        self.in_billing     = False

    def next_seq(self) -> int:
        self.session_seq += 1
        return self.session_seq


class ReIDCache:
    """Stores exited tracks for re-entry matching."""

    def __init__(self):
        self._cache: list[dict] = []   # list of {visitor_id, exit_time, histogram}

    def add_exit(self, track: VisitorTrack, exit_time: datetime):
        self._cache.append({
            "visitor_id": track.visitor_id,
            "exit_time":  exit_time,
            "histogram":  track.histogram,
        })
        # prune old entries
        cutoff = exit_time - timedelta(seconds=RE_ID_WINDOW_SEC)
        self._cache = [e for e in self._cache if e["exit_time"] > cutoff]

    def find_match(self, histogram: Optional[np.ndarray],
                   now: datetime) -> Optional[str]:
        if histogram is None:
            return None
        cutoff = now - timedelta(seconds=RE_ID_WINDOW_SEC)
        best_sim, best_vid = 0.0, None
        for entry in reversed(self._cache):
            if entry["exit_time"] < cutoff:
                continue
            sim = _cosine_sim(histogram, entry["histogram"])
            if sim > best_sim:
                best_sim = sim
                best_vid = entry["visitor_id"]
        if best_sim >= RE_ID_THRESHOLD:
            return best_vid
        return None


class TrackerState:
    """Manages all active visitor tracks for one video processing session."""

    def __init__(self, store_id: str, camera_id: str, clip_duration_sec: float):
        self.store_id          = store_id
        self.camera_id         = camera_id
        self.clip_duration_sec = clip_duration_sec
        self.active: dict[int, VisitorTrack] = {}   # track_id → track
        self.reid_cache        = ReIDCache()
        self._uid_counter      = 0

    def _new_visitor_id(self) -> str:
        self._uid_counter += 1
        return _make_visitor_id(f"{self.store_id}_{self.camera_id}_{self._uid_counter}")

    def get_or_create(self, track_id: int, confidence: float,
                      frame: np.ndarray, box: list[float],
                      now: datetime) -> tuple[VisitorTrack, bool, bool]:
        """
        Returns (track, is_new, is_reentry).
        is_new     → should emit ENTRY
        is_reentry → should emit REENTRY instead of ENTRY
        """
        if track_id in self.active:
            t = self.active[track_id]
            t.last_seen  = now
            t.confidence = max(t.confidence, confidence)
            if frame is not None:
                t.histogram = _colour_histogram(frame, box)
            return t, False, False

        histogram = _colour_histogram(frame, box) if frame is not None else None
        matched_vid = self.reid_cache.find_match(histogram, now)
        is_reentry  = matched_vid is not None
        visitor_id  = matched_vid if is_reentry else self._new_visitor_id()

        track = VisitorTrack(
            track_id   = track_id,
            visitor_id = visitor_id,
            store_id   = self.store_id,
            camera_id  = self.camera_id,
            entry_time = now,
            confidence = confidence,
            histogram  = histogram,
        )
        self.active[track_id] = track
        return track, True, is_reentry

    def mark_exited(self, track_id: int, exit_time: datetime):
        if track_id in self.active:
            t = self.active.pop(track_id)
            t.exited    = True
            t.exit_time = exit_time
            self.reid_cache.add_exit(t, exit_time)

    def finalise_staff(self):
        """After clip ends, flag tracks active for >65% of clip as staff."""
        for t in self.active.values():
            duration = (t.last_seen - t.entry_time).total_seconds()
            if duration > self.clip_duration_sec * STAFF_DWELL_RATIO:
                t.is_staff = True

    def get_zone_dwell_due(self, track: VisitorTrack,
                           now: datetime) -> Optional[int]:
        """
        Returns dwell_ms if a ZONE_DWELL event should be emitted, else None.
        Emits every DWELL_EMIT_INTERVAL seconds of continuous zone presence.
        """
        if track.current_zone is None or track.zone_entry_time is None:
            return None
        elapsed = (now - track.zone_entry_time).total_seconds()
        if elapsed < DWELL_EMIT_INTERVAL:
            return None
        last = track.last_dwell_emit
        if last is None or (now - last).total_seconds() >= DWELL_EMIT_INTERVAL:
            return int(elapsed * 1000)
        return None
