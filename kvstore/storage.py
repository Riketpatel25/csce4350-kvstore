"""Append-only log that persists every write to disk.

Each record is one JSON array per line, e.g. ``["SET", "name", "riket"]``.
Records are flushed and fsync'd before a command returns, so a crash or
kill never loses an acknowledged write. On startup the log is replayed
from the top to rebuild the in-memory index.
"""

from __future__ import annotations

import json
import os
from collections.abc import Iterator

Record = list


class AppendLog:
    """Write-ahead, append-only log file (``data.db`` by default)."""

    def __init__(self, path: str) -> None:
        self.path = path

    def append(self, records: list[Record]) -> None:
        """Durably append one or more records (one batch = one fsync)."""
        if not records:
            return
        lines = "".join(json.dumps(r, separators=(",", ":")) + "\n" for r in records)
        with open(self.path, "a", encoding="utf-8") as fh:
            fh.write(lines)
            fh.flush()
            os.fsync(fh.fileno())

    def replay(self) -> Iterator[Record]:
        """Yield records in write order. A torn last line (crash mid-write) is skipped."""
        if not os.path.exists(self.path):
            return
        with open(self.path, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(record, list) and record:
                    yield record
