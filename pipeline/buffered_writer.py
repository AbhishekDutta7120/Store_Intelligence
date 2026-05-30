from pipeline.emit import EventWriter

class BufferedEventWriter:
    def __init__(self, output_path: str, api_url: str = None):
        self.writer = EventWriter(output_path, api_url)
        self.buffer = []

    def write(self, event: dict):
        self.buffer.append(event)

    def flush_all(self, state):
        """Update is_staff before actually writing."""
        for event in self.buffer:
            if event["visitor_id"] in state.staff_visitor_ids:
                event["is_staff"] = True
            self.writer.write(event)
        self.buffer.clear()
        self.writer.flush_to_api()

    def close(self):
        self.writer.close()
