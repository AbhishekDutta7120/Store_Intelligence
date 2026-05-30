import uuid
from datetime import datetime, timedelta

GROUP_TIME_WINDOW_SEC = 3.0
GROUP_DIST_THRESHOLD = 0.2  # normalized distance

class GroupManager:
    def __init__(self):
        self.recent_entries = [] # list of dicts: {'visitor_id': str, 'timestamp': datetime, 'cx': float, 'cy': float, 'group_id': str}

    def process_entry(self, visitor_id: str, timestamp: datetime, cx: float, cy: float) -> str:
        # clear old entries
        cutoff = timestamp - timedelta(seconds=GROUP_TIME_WINDOW_SEC)
        self.recent_entries = [e for e in self.recent_entries if e['timestamp'] >= cutoff]

        # find if there is a recent entry near this one
        matched_group_id = None
        for entry in self.recent_entries:
            dist = ((entry['cx'] - cx)**2 + (entry['cy'] - cy)**2)**0.5
            if dist < GROUP_DIST_THRESHOLD:
                matched_group_id = entry['group_id']
                break

        if not matched_group_id:
            matched_group_id = "GRP_" + str(uuid.uuid4())[:8]

        self.recent_entries.append({
            'visitor_id': visitor_id,
            'timestamp': timestamp,
            'cx': cx,
            'cy': cy,
            'group_id': matched_group_id
        })
        return matched_group_id
