"""Core key-value store: strings, hashes, lists, counters, TTLs and transactions.

Every change is described as a small log record (SET, DEL, HSET, ...).
The same ``_apply`` function is used both for live commands and for
replaying the log on startup, so the in-memory state after a restart is
exactly the state before it.
"""

from __future__ import annotations

import math
import time
from typing import Callable

from .index import HashIndex
from .storage import AppendLog, Record


class KVError(Exception):
    """A user-facing command error (printed as ``ERR <message>``)."""


WRONGTYPE = "WRONGTYPE operation against a key holding the wrong kind of value"


class Database:
    def __init__(self, path: str = "data.db", clock: Callable[[], float] = time.time) -> None:
        self._log = AppendLog(path)
        self._clock = clock
        self._data = HashIndex()
        self._expiry = HashIndex()  # key -> absolute expiry time (epoch seconds)
        self._pending: list[Record] | None = None  # buffered records while in a transaction
        self._saved: tuple[HashIndex, HashIndex] | None = None  # state to restore on ABORT
        for record in self._log.replay():
            self._apply(record)

    # ------------------------------------------------------------------ internals

    def _apply(self, record: Record) -> None:
        """Apply one log record to the in-memory index."""
        op, args = record[0], record[1:]
        if op == "SET":
            self._data.put(args[0], args[1])
            self._expiry.delete(args[0])  # SET clears any TTL
        elif op == "DEL":
            self._data.delete(args[0])
            self._expiry.delete(args[0])
        elif op == "EXPIREAT":
            self._expiry.put(args[0], float(args[1]))
        elif op == "HSET":
            table = self._data.get(args[0])
            if not isinstance(table, HashIndex):
                table = HashIndex()
                self._data.put(args[0], table)
            table.put(args[1], args[2])
        elif op in ("LPUSH", "RPUSH"):
            items = self._data.get(args[0])
            if not isinstance(items, list):
                items = []
                self._data.put(args[0], items)
            if op == "LPUSH":
                items.insert(0, args[1])
            else:
                items.append(args[1])
        elif op in ("LPOP", "RPOP"):
            items = self._data.get(args[0])
            if isinstance(items, list) and items:
                items.pop(0 if op == "LPOP" else -1)
                if not items:
                    self._data.delete(args[0])
        elif op == "FLUSHDB":
            self._data.clear()
            self._expiry.clear()

    def _write(self, *records: Record) -> None:
        """Apply records now and persist them (or buffer them inside a transaction)."""
        for record in records:
            self._apply(record)
        if self._pending is not None:
            self._pending.extend(records)
        else:
            self._log.append(list(records))

    def _is_expired(self, key: str) -> bool:
        deadline = self._expiry.get(key)
        return deadline is not None and self._clock() >= deadline

    def _lookup(self, key: str):
        """Return the live value for key, lazily dropping it if its TTL passed."""
        if self._is_expired(key):
            self._data.delete(key)
            self._expiry.delete(key)
            return None
        return self._data.get(key)

    def _string(self, key: str) -> str | None:
        value = self._lookup(key)
        if value is not None and not isinstance(value, str):
            raise KVError(WRONGTYPE)
        return value

    def _hash(self, key: str) -> HashIndex | None:
        value = self._lookup(key)
        if value is not None and not isinstance(value, HashIndex):
            raise KVError(WRONGTYPE)
        return value

    def _list(self, key: str) -> list | None:
        value = self._lookup(key)
        if value is not None and not isinstance(value, list):
            raise KVError(WRONGTYPE)
        return value

    # ------------------------------------------------------------------ strings

    def set(self, key: str, value: str) -> None:
        self._write(["SET", key, value])

    def get(self, key: str) -> str | None:
        return self._string(key)

    def mset(self, pairs: list[tuple[str, str]]) -> None:
        self._write(*(["SET", k, v] for k, v in pairs))

    def mget(self, keys: list[str]) -> list[str | None]:
        result = []
        for key in keys:
            value = self._lookup(key)
            result.append(value if isinstance(value, str) else None)
        return result

    def delete(self, keys: list[str]) -> int:
        existing = [k for k in keys if self._lookup(k) is not None]
        self._write(*(["DEL", k] for k in existing))
        return len(existing)

    def exists(self, keys: list[str]) -> int:
        return sum(1 for k in keys if self._lookup(k) is not None)

    def incr_by(self, key: str, amount: int) -> int:
        current = self._string(key)
        try:
            number = int(current) if current is not None else 0
        except ValueError:
            raise KVError("value is not an integer or out of range") from None
        number += amount
        ttl = self._expiry.get(key)
        records: list[Record] = [["SET", key, str(number)]]
        if ttl is not None:  # INCR keeps an existing TTL, unlike SET
            records.append(["EXPIREAT", key, ttl])
        self._write(*records)
        return number

    # ------------------------------------------------------------------ expiration

    def expire(self, key: str, seconds: int) -> int:
        if self._lookup(key) is None:
            return 0
        if seconds <= 0:
            self._write(["DEL", key])
        else:
            self._write(["EXPIREAT", key, self._clock() + seconds])
        return 1

    def ttl(self, key: str) -> int:
        """Seconds left; -1 if the key has no TTL, -2 if it does not exist."""
        if self._lookup(key) is None:
            return -2
        deadline = self._expiry.get(key)
        if deadline is None:
            return -1
        return max(0, math.ceil(deadline - self._clock()))

    # ------------------------------------------------------------------ range

    def range(self, start: str, end: str) -> list[str]:
        """Keys between start and end inclusive, sorted. Empty bound = unbounded."""
        keys = [k for k in list(self._data.keys()) if self._lookup(k) is not None]
        return sorted(k for k in keys if (not start or k >= start) and (not end or k <= end))

    # ------------------------------------------------------------------ hashes

    def hset(self, key: str, field: str, value: str) -> int:
        table = self._hash(key)
        is_new = table is None or field not in table
        self._write(["HSET", key, field, value])
        return 1 if is_new else 0

    def hget(self, key: str, field: str) -> str | None:
        table = self._hash(key)
        return None if table is None else table.get(field)

    def hgetall(self, key: str) -> list[tuple[str, str]]:
        table = self._hash(key)
        return [] if table is None else list(table.items())

    # ------------------------------------------------------------------ lists

    def push(self, key: str, values: list[str], left: bool) -> int:
        self._list(key)  # type check
        op = "LPUSH" if left else "RPUSH"
        self._write(*([op, key, v] for v in values))
        return len(self._list(key) or [])

    def pop(self, key: str, left: bool) -> str | None:
        items = self._list(key)
        if not items:
            return None
        value = items[0] if left else items[-1]
        self._write(["LPOP" if left else "RPOP", key])
        return value

    def lrange(self, key: str, start: int, stop: int) -> list[str]:
        items = self._list(key) or []
        n = len(items)
        if start < 0:
            start = max(0, n + start)
        if stop < 0:
            stop = n + stop
        return items[start : stop + 1] if start <= stop else []

    # ------------------------------------------------------------------ management

    def flushdb(self) -> None:
        self._write(["FLUSHDB"])

    # ------------------------------------------------------------------ transactions

    @property
    def in_transaction(self) -> bool:
        return self._pending is not None

    def begin(self) -> None:
        if self._pending is not None:
            raise KVError("BEGIN inside a transaction is not allowed")
        self._saved = (self._data.copy(), self._expiry.copy())
        self._pending = []

    def commit(self) -> None:
        if self._pending is None:
            raise KVError("COMMIT without BEGIN")
        records, self._pending, self._saved = self._pending, None, None
        self._log.append(records)  # one fsync for the whole block

    def abort(self) -> None:
        if self._pending is None or self._saved is None:
            raise KVError("ABORT without BEGIN")
        self._data, self._expiry = self._saved
        self._pending, self._saved = None, None
