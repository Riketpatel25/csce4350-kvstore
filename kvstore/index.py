"""A hand-written hash table used as the in-memory index.

The assignment forbids relying on the language's built-in dictionary/map
types, so keys are stored in a fixed number of buckets (plain lists) and
looked up with separate chaining. The table doubles in size when it gets
too full so lookups stay close to O(1).
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

_INITIAL_BUCKETS = 64
_MAX_LOAD_FACTOR = 0.75


class HashIndex:
    """Maps string keys to arbitrary values using separate chaining."""

    def __init__(self, capacity: int = _INITIAL_BUCKETS) -> None:
        self._buckets: list[list[list[Any]]] = [[] for _ in range(capacity)]
        self._size = 0

    def __len__(self) -> int:
        return self._size

    def __contains__(self, key: str) -> bool:
        return self._find(key) is not None

    def _bucket(self, key: str) -> list[list[Any]]:
        return self._buckets[hash(key) % len(self._buckets)]

    def _find(self, key: str) -> list[Any] | None:
        """Return the [key, value] entry for key, or None."""
        for entry in self._bucket(key):
            if entry[0] == key:
                return entry
        return None

    def get(self, key: str, default: Any = None) -> Any:
        entry = self._find(key)
        return default if entry is None else entry[1]

    def put(self, key: str, value: Any) -> bool:
        """Insert or replace a value. Returns True if the key is new."""
        entry = self._find(key)
        if entry is not None:
            entry[1] = value  # last write wins
            return False
        self._bucket(key).append([key, value])
        self._size += 1
        if self._size > len(self._buckets) * _MAX_LOAD_FACTOR:
            self._resize(len(self._buckets) * 2)
        return True

    def delete(self, key: str) -> bool:
        """Remove a key. Returns True if it existed."""
        bucket = self._bucket(key)
        for i, entry in enumerate(bucket):
            if entry[0] == key:
                bucket.pop(i)
                self._size -= 1
                return True
        return False

    def clear(self) -> None:
        self._buckets = [[] for _ in range(_INITIAL_BUCKETS)]
        self._size = 0

    def items(self) -> Iterator[tuple[str, Any]]:
        for bucket in self._buckets:
            yield from ((key, value) for key, value in bucket)

    def keys(self) -> Iterator[str]:
        for key, _ in self.items():
            yield key

    def copy(self) -> HashIndex:
        """Deep enough copy for transactions: nested hashes/lists are copied too."""
        clone = HashIndex(len(self._buckets))
        for key, value in self.items():
            if isinstance(value, HashIndex):
                value = value.copy()
            elif isinstance(value, list):
                value = list(value)
            clone.put(key, value)
        return clone

    def _resize(self, capacity: int) -> None:
        old = list(self.items())
        self._buckets = [[] for _ in range(capacity)]
        self._size = 0
        for key, value in old:
            self.put(key, value)
