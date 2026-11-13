"""Command-line front end: reads commands from STDIN, writes replies to STDOUT.

Output rules (kept simple so the black-box tester can read them):
  * one reply per command, flushed immediately
  * a missing value prints an empty line
  * multi-value replies print one item per line; RANGE ends with ``END``
  * errors print ``ERR <message>``
"""

from __future__ import annotations

import shlex
import sys
from typing import Callable, TextIO

from .database import Database, KVError

Handler = Callable[[Database, list[str]], list[str]]


def _value(v: str | None) -> str:
    return "" if v is None else v


def _need(args: list[str], count: int, usage: str, exact: bool = True) -> None:
    if (exact and len(args) != count) or (not exact and len(args) < count):
        raise KVError(f"usage: {usage}")


def _int(text: str) -> int:
    try:
        return int(text)
    except ValueError:
        raise KVError("value is not an integer or out of range") from None


def _set(db: Database, a: list[str]) -> list[str]:
    _need(a, 2, "SET <key> <value>")
    db.set(a[0], a[1])
    return ["OK"]


def _get(db: Database, a: list[str]) -> list[str]:
    _need(a, 1, "GET <key>")
    return [_value(db.get(a[0]))]


def _del(db: Database, a: list[str]) -> list[str]:
    _need(a, 1, "DEL <key> [key ...]", exact=False)
    return [str(db.delete(a))]


def _exists(db: Database, a: list[str]) -> list[str]:
    _need(a, 1, "EXISTS <key> [key ...]", exact=False)
    return [str(db.exists(a))]


def _mset(db: Database, a: list[str]) -> list[str]:
    if not a or len(a) % 2:
        raise KVError("usage: MSET <k1> <v1> [k2 v2 ...]")
    db.mset(list(zip(a[0::2], a[1::2])))
    return ["OK"]


def _mget(db: Database, a: list[str]) -> list[str]:
    _need(a, 1, "MGET <k1> [k2 ...]", exact=False)
    return [_value(v) for v in db.mget(a)]


def _expire(db: Database, a: list[str]) -> list[str]:
    _need(a, 2, "EXPIRE <key> <seconds>")
    return [str(db.expire(a[0], _int(a[1])))]


def _ttl(db: Database, a: list[str]) -> list[str]:
    _need(a, 1, "TTL <key>")
    return [str(db.ttl(a[0]))]


def _range(db: Database, a: list[str]) -> list[str]:
    _need(a, 2, "RANGE <start> <end>")
    return db.range(a[0], a[1]) + ["END"]


def _begin(db: Database, a: list[str]) -> list[str]:
    db.begin()
    return ["OK"]


def _commit(db: Database, a: list[str]) -> list[str]:
    db.commit()
    return ["OK"]


def _abort(db: Database, a: list[str]) -> list[str]:
    db.abort()
    return ["OK"]


def _hset(db: Database, a: list[str]) -> list[str]:
    _need(a, 3, "HSET <hash> <field> <value>")
    return [str(db.hset(a[0], a[1], a[2]))]


def _hget(db: Database, a: list[str]) -> list[str]:
    _need(a, 2, "HGET <hash> <field>")
    return [_value(db.hget(a[0], a[1]))]


def _hgetall(db: Database, a: list[str]) -> list[str]:
    _need(a, 1, "HGETALL <hash>")
    lines: list[str] = []
    for field, value in db.hgetall(a[0]):
        lines += [field, value]
    return lines or [""]


def _push(left: bool) -> Handler:
    def handler(db: Database, a: list[str]) -> list[str]:
        _need(a, 2, ("LPUSH" if left else "RPUSH") + " <key> <value> [value ...]", exact=False)
        return [str(db.push(a[0], a[1:], left))]

    return handler


def _pop(left: bool) -> Handler:
    def handler(db: Database, a: list[str]) -> list[str]:
        _need(a, 1, ("LPOP" if left else "RPOP") + " <key>")
        return [_value(db.pop(a[0], left))]

    return handler


def _lrange(db: Database, a: list[str]) -> list[str]:
    _need(a, 3, "LRANGE <key> <start> <stop>")
    return db.lrange(a[0], _int(a[1]), _int(a[2])) or [""]


def _incr(amount: int) -> Handler:
    def handler(db: Database, a: list[str]) -> list[str]:
        _need(a, 1, ("INCR" if amount > 0 else "DECR") + " <key>")
        return [str(db.incr_by(a[0], amount))]

    return handler


def _flushdb(db: Database, a: list[str]) -> list[str]:
    db.flushdb()
    return ["OK"]


# Command table (a list of pairs, searched linearly -- no dict needed).
COMMANDS: list[tuple[str, Handler]] = [
    ("SET", _set), ("GET", _get), ("DEL", _del), ("EXISTS", _exists),
    ("MSET", _mset), ("MGET", _mget), ("EXPIRE", _expire), ("TTL", _ttl),
    ("RANGE", _range), ("BEGIN", _begin), ("COMMIT", _commit), ("ABORT", _abort),
    ("HSET", _hset), ("HGET", _hget), ("HGETALL", _hgetall),
    ("LPUSH", _push(True)), ("RPUSH", _push(False)),
    ("LPOP", _pop(True)), ("RPOP", _pop(False)), ("LRANGE", _lrange),
    ("INCR", _incr(1)), ("DECR", _incr(-1)), ("FLUSHDB", _flushdb),
]  # fmt: skip


def parse(line: str) -> list[str]:
    """Split a command line; double/single quotes group words, "" is an empty arg."""
    try:
        return shlex.split(line)
    except ValueError:  # unbalanced quote -> fall back to plain whitespace split
        return line.split()


def execute(db: Database, line: str) -> list[str] | None:
    """Run one command line. Returns reply lines, or None when the user typed EXIT."""
    parts = parse(line)
    if not parts:
        return []
    name, args = parts[0].upper(), parts[1:]
    if name in ("EXIT", "QUIT"):
        return None
    for command, handler in COMMANDS:
        if command == name:
            try:
                return handler(db, args)
            except KVError as exc:
                return [f"ERR {exc}"]
    return [f"ERR unknown command '{parts[0]}'"]


def run(stdin: TextIO = sys.stdin, stdout: TextIO = sys.stdout, path: str = "data.db") -> int:
    db = Database(path)
    for line in stdin:
        reply = execute(db, line)
        if reply is None:
            break
        if reply:
            stdout.write("\n".join(reply) + "\n")  # one write per reply
            stdout.flush()
    return 0
