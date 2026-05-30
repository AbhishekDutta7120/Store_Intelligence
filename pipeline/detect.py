"""
detect.py — Main CCTV detection pipeline.

Usage:
    python pipeline/detect.py --videos CAM_1.mp4 CAM_2.mp4 ... \
                               --layout config/store_layout.json \
                               --output data/events.jsonl \
                               [--api-url http://localhost:8000/events/ingest]

CPU optimisation:
  - YOLOv8n (nano): fastest model, good enough for 1080p retail footage.
  - Frame skip: processes every FRAME_SKIP-th frame (default 3).
  - Resize: downscales to INFER_SIZE (640) before inference; scales detections back.
  - ByteTrack: built-in to ultralytics, no extra install needed.

Zone detection:
  - Zones are defined as normalised [x1, y1, x2, y2] bboxes in store_layout.json.
  - Entry/exit detection uses a virtual line at y = ENTRY_LINE_RATIO of frame height.
  - Direction determined by centroid crossing the line inward vs outward.
"""

import argparse
import json
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path

# ── constants ────────────────────────────────────────────────────────────────
FRAME_SKIP       = 3      # process 1 in every N frames
INFER_SIZE       = 640    # inference resolution (longest side)
ENTRY_LINE_RATIO = 0.55   # y-position of virtual entry/exit line (as fraction of height)
CONF_THRESHOLD   = 0.35   # YOLO confidence threshold — keep low, flag in event
TRACK_GONE_SEC   = 3.0    # seconds without detection before track considered gone
CLIP_START_TIME  = datetime(2026, 4, 10, 11, 0, 0, tzinfo=timezone.utc)  # Brigade Bangalore April 10 2026

# ── optional: set to your API URL to live-ingest ─────────────────────────────
DEFAULT_API_URL  = "http://localhost:8000/events/ingest"


def load_layout(layout_path: str) -> dict:
    with open(layout_path, encoding="utf-8") as f:
        layout = json.load(f)
    # Build camera_id → store config lookup
    cam_map = {}
    for store in layout["stores"]:
        for cam in store["cameras"]:
            cam_map[cam["camera_id"]] = {
                "store_id":  store["store_id"],
                "cam_type":  cam["type"],       # entry_exit | floor | billing
                "zones":     [z for z in store["zones"] if z["camera_id"] == cam["camera_id"]],
            }
    return cam_map


def get_zone_for_centroid(cx_norm: float, cy_norm: float,
                          zones: list[dict]) -> tuple[str | None, str | None]:
    """Return (zone_id, sku_zone) for a normalised centroid, or (None, None)."""
    for zone in zones:
        x1, y1, x2, y2 = zone["bbox"]
        if x1 <= cx_norm <= x2 and y1 <= cy_norm <= y2:
            return zone["zone_id"], zone.get("sku_zone")
    return None, None


def process_video(video_path: Path, camera_id: str, cam_config: dict,
                  writer, pos_lookup: dict):
    """Process one video clip and emit events via writer."""
    try:
        from ultralytics import YOLO
        import cv2
    except ImportError:
        print("ERROR: Install ultralytics and opencv-python-headless")
        print("  pip install ultralytics opencv-python-headless")
        sys.exit(1)

    from pipeline.tracker import TrackerState, DWELL_EMIT_INTERVAL
    from pipeline.emit import make_event

    store_id  = cam_config["store_id"]
    cam_type  = cam_config["cam_type"]
    zones     = cam_config["zones"]

    cap = cv2.VideoCapture(str(video_path))
    if not cap.isOpened():
        print(f"[ERROR] Cannot open {video_path}")
        return

    fps           = cap.get(cv2.CAP_PROP_FPS) or 15.0
    total_frames  = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
    clip_duration = total_frames / fps
    clip_start    = CLIP_START_TIME

    print(f"[INFO] Processing {video_path.name} | {total_frames} frames | {clip_duration:.0f}s | cam_type={cam_type}")

    model   = YOLO("yolov8n.pt")
    state   = TrackerState(store_id, camera_id, clip_duration)

    # Track last known centroids for direction detection
    prev_cy: dict[int, float] = {}   # track_id → previous normalised cy

    # Track IDs seen in current frame
    frame_idx  = 0
    last_seen_frames: dict[int, int] = {}  # track_id → last frame_idx seen

    while True:
        ret, frame = cap.read()
        if not ret:
            break
        frame_idx += 1

        # Skip frames for CPU speed
        if frame_idx % FRAME_SKIP != 0:
            continue

        frame_sec = frame_idx / fps
        now       = clip_start + timedelta(seconds=frame_sec)
        h, w      = frame.shape[:2]

        # Resize for faster inference
        scale     = INFER_SIZE / max(h, w)
        new_w     = int(w * scale)
        new_h     = int(h * scale)
        small     = cv2.resize(frame, (new_w, new_h))

        # Run YOLO + ByteTrack (classes=[0] = person only)
        results = model.track(
            small,
            persist    = True,
            classes    = [0],
            conf       = CONF_THRESHOLD,
            verbose    = False,
            tracker    = "bytetrack.yaml",
        )

        active_ids_this_frame = set()

        if results and results[0].boxes is not None and results[0].boxes.id is not None:
            boxes_xyxy = results[0].boxes.xyxy.cpu().numpy()
            track_ids  = results[0].boxes.id.int().cpu().tolist()
            confs      = results[0].boxes.conf.cpu().tolist()

            for tid, conf, box in zip(track_ids, confs, boxes_xyxy):
                active_ids_this_frame.add(tid)
                last_seen_frames[tid] = frame_idx

                # Scale box back to original resolution
                x1 = box[0] / scale
                y1 = box[1] / scale
                x2 = box[2] / scale
                y2 = box[3] / scale
                orig_box = [x1, y1, x2, y2]

                cx_norm = ((x1 + x2) / 2) / w
                cy_norm = ((y1 + y2) / 2) / h

                track, is_new, is_reentry = state.get_or_create(
                    tid, conf, frame, orig_box, now
                )

                # ── Entry/Exit detection (entry_exit cameras only) ──────────
                if cam_type == "entry_exit":
                    if is_new:
                        # Determine entry direction from position: if cy_norm > ENTRY_LINE_RATIO
                        # person appeared below line → entering store
                        event_type = "REENTRY" if is_reentry else "ENTRY"
                        writer.write(make_event(
                            store_id   = store_id,
                            camera_id  = camera_id,
                            visitor_id = track.visitor_id,
                            event_type = event_type,
                            timestamp  = now,
                            zone_id    = None,
                            dwell_ms   = 0,
                            is_staff   = track.is_staff,
                            confidence = conf,
                            session_seq= track.next_seq(),
                        ))

                    # Track direction for exit detection
                    if tid in prev_cy:
                        prev = prev_cy[tid]
                        # Crossing line outward (cy decreasing past threshold) → EXIT
                        if prev > ENTRY_LINE_RATIO and cy_norm < ENTRY_LINE_RATIO:
                            writer.write(make_event(
                                store_id   = store_id,
                                camera_id  = camera_id,
                                visitor_id = track.visitor_id,
                                event_type = "EXIT",
                                timestamp  = now,
                                dwell_ms   = 0,
                                is_staff   = track.is_staff,
                                confidence = conf,
                                session_seq= track.next_seq(),
                            ))
                            state.mark_exited(tid, now)
                            active_ids_this_frame.discard(tid)

                    prev_cy[tid] = cy_norm

                # ── Zone detection (floor + billing cameras) ────────────────
                else:
                    zone_id, sku_zone = get_zone_for_centroid(cx_norm, cy_norm, zones)

                    if is_new and zone_id:
                        writer.write(make_event(
                            store_id   = store_id,
                            camera_id  = camera_id,
                            visitor_id = track.visitor_id,
                            event_type = "ZONE_ENTER",
                            timestamp  = now,
                            zone_id    = zone_id,
                            dwell_ms   = 0,
                            is_staff   = track.is_staff,
                            confidence = conf,
                            sku_zone   = sku_zone,
                            session_seq= track.next_seq(),
                        ))
                        track.current_zone     = zone_id
                        track.zone_entry_time  = now

                    elif not is_new and zone_id != track.current_zone:
                        # Zone transition
                        if track.current_zone:
                            writer.write(make_event(
                                store_id   = store_id,
                                camera_id  = camera_id,
                                visitor_id = track.visitor_id,
                                event_type = "ZONE_EXIT",
                                timestamp  = now,
                                zone_id    = track.current_zone,
                                dwell_ms   = int((now - track.zone_entry_time).total_seconds() * 1000)
                                             if track.zone_entry_time else 0,
                                is_staff   = track.is_staff,
                                confidence = conf,
                                session_seq= track.next_seq(),
                            ))
                        if zone_id:
                            # Billing queue detection
                            is_billing = (zone_id == "BILLING")
                            q_depth    = _estimate_queue_depth(active_ids_this_frame, state) if is_billing else None
                            evt_type   = "BILLING_QUEUE_JOIN" if (is_billing and q_depth and q_depth > 0) else "ZONE_ENTER"

                            writer.write(make_event(
                                store_id   = store_id,
                                camera_id  = camera_id,
                                visitor_id = track.visitor_id,
                                event_type = evt_type,
                                timestamp  = now,
                                zone_id    = zone_id,
                                dwell_ms   = 0,
                                is_staff   = track.is_staff,
                                confidence = conf,
                                queue_depth= q_depth,
                                sku_zone   = sku_zone,
                                session_seq= track.next_seq(),
                            ))
                            track.current_zone     = zone_id
                            track.zone_entry_time  = now
                            track.last_dwell_emit  = None

                    # ZONE_DWELL: emit every 30s of continuous presence
                    dwell_ms = state.get_zone_dwell_due(track, now)
                    if dwell_ms and zone_id == track.current_zone:
                        writer.write(make_event(
                            store_id   = store_id,
                            camera_id  = camera_id,
                            visitor_id = track.visitor_id,
                            event_type = "ZONE_DWELL",
                            timestamp  = now,
                            zone_id    = zone_id,
                            dwell_ms   = dwell_ms,
                            is_staff   = track.is_staff,
                            confidence = conf,
                            sku_zone   = sku_zone,
                            session_seq= track.next_seq(),
                        ))
                        track.last_dwell_emit = now

        # ── Detect gone tracks ──────────────────────────────────────────────
        gone_sec = TRACK_GONE_SEC * fps / FRAME_SKIP
        for tid in list(last_seen_frames.keys()):
            if tid not in active_ids_this_frame:
                frames_ago = frame_idx - last_seen_frames[tid]
                if frames_ago > gone_sec and tid in state.active:
                    track = state.active[tid]
                    gone_time = clip_start + timedelta(seconds=last_seen_frames[tid] / fps)
                    # Emit BILLING_QUEUE_ABANDON if they were in billing but no purchase followed
                    if cam_type == "billing" and track.in_billing:
                        writer.write(make_event(
                            store_id   = store_id,
                            camera_id  = camera_id,
                            visitor_id = track.visitor_id,
                            event_type = "BILLING_QUEUE_ABANDON",
                            timestamp  = gone_time,
                            zone_id    = "BILLING",
                            dwell_ms   = 0,
                            is_staff   = track.is_staff,
                            confidence = track.confidence,
                            session_seq= track.next_seq(),
                        ))
                    state.mark_exited(tid, gone_time)
                    del last_seen_frames[tid]
                    prev_cy.pop(tid, None)

    cap.release()
    state.finalise_staff()
    print(f"[INFO] Done: {video_path.name}")


def _estimate_queue_depth(active_ids: set, state) -> int:
    """Count active tracks in billing zone."""
    count = 0
    for tid in active_ids:
        t = state.active.get(tid)
        if t and t.current_zone == "BILLING" and not t.is_staff:
            count += 1
    return count


def map_filename_to_camera(filename: str, layout_path: str) -> str | None:
    """Heuristically map a video filename like 'CAM_1.mp4' to a camera_id."""
    with open(layout_path) as f:
        layout = json.load(f)
    all_cameras = []
    for store in layout["stores"]:
        all_cameras.extend(cam["camera_id"] for cam in store["cameras"])

    stem = Path(filename).stem.replace(" ", "_").upper()
    if stem in all_cameras:
        return stem

    # Try numeric suffix matching: CAM_1 → CAM_1, CAM 1 → CAM_1
    import re
    m = re.search(r"(\d+)", stem)
    if m:
        num = m.group(1)
        for cid in all_cameras:
            if cid.endswith(f"_{num}") or cid.endswith(num):
                return cid

    print(f"[WARN] Cannot map {filename} to a camera_id. Using CAM_{stem}")
    return f"CAM_{stem}"


def main():
    ap = argparse.ArgumentParser(description="Store Intelligence Detection Pipeline")
    ap.add_argument("--videos",   nargs="+", required=True, help="Paths to video files")
    ap.add_argument("--layout",   default="config/store_layout.json")
    ap.add_argument("--output",   default="data/events.jsonl")
    ap.add_argument("--api-url",  default=None, help="POST events to this URL")
    ap.add_argument("--pos",      default="data/pos_transactions.csv")
    args = ap.parse_args()

    cam_map = load_layout(args.layout)

    # Load POS data for correlation
    pos_lookup = {}
    pos_path = Path(args.pos)
    if pos_path.exists():
        import csv
        with open(pos_path) as f:
            for row in csv.DictReader(f):
                pos_lookup.setdefault(row["store_id"], []).append(row)

    from pipeline.emit import EventWriter
    writer = EventWriter(args.output, args.api_url)

    for video_str in args.videos:
        video_path = Path(video_str)
        camera_id  = map_filename_to_camera(video_path.name, args.layout)
        if camera_id not in cam_map:
            print(f"[ERROR] camera_id '{camera_id}' not in layout. Skipping.")
            continue
        process_video(video_path, camera_id, cam_map[camera_id], writer, pos_lookup)

    writer.close()
    print(f"[DONE] Events written to {args.output}")


if __name__ == "__main__":
    main()
