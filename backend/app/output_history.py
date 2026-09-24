"""Bounded recent text cache plus uncommitted entries for durable retry."""
RECENT_OUTPUT_LIMIT = 1000
OUTPUT_PAGE_SIZE = 300
OUTPUT_MAX_LIMIT = 1000


class OutputCache(list):
    def __init__(self, lines=(), next_sequence=0):
        super().__init__(list(lines)[-RECENT_OUTPUT_LIMIT:])
        self.next_sequence = next_sequence
        self.pending = []
        self.dirty = False
        self.hold = False

    def append(self, text):
        self.pending.append((self.next_sequence, text))
        self.next_sequence += 1
        super().append(text)
        del self[:-RECENT_OUTPUT_LIMIT]

    def extend(self, lines):
        for text in lines:
            self.append(text)
