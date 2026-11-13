"""Unit and black-box tests. Run with: python3 -m unittest discover -s tests"""

import os
import subprocess
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from kvstore.cli import execute  # noqa: E402
from kvstore.database import Database  # noqa: E402
from kvstore.index import HashIndex  # noqa: E402


class FakeClock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


class HashIndexTest(unittest.TestCase):
    def test_put_get_delete_and_resize(self):
        index = HashIndex(capacity=4)
        for i in range(500):
            index.put(f"k{i}", i)
        self.assertEqual(len(index), 500)
        self.assertEqual(index.get("k250"), 250)
        self.assertFalse(index.put("k1", "new"))  # last write wins
        self.assertEqual(index.get("k1"), "new")
        self.assertTrue(index.delete("k1"))
        self.assertFalse(index.delete("k1"))
        self.assertIsNone(index.get("k1"))


class DatabaseTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.tmp.name, "data.db")
        self.clock = FakeClock()
        self.db = Database(self.path, clock=self.clock)

    def tearDown(self):
        self.tmp.cleanup()

    def reopen(self) -> Database:
        return Database(self.path, clock=self.clock)

    def run_cmd(self, line: str) -> list:
        return execute(self.db, line)

    def test_set_get_overwrite_and_missing(self):
        self.assertEqual(self.run_cmd("SET a 1"), ["OK"])
        self.run_cmd("SET a 2")
        self.assertEqual(self.run_cmd("GET a"), ["2"])
        self.assertEqual(self.run_cmd("GET nope"), [""])
        self.assertTrue(os.path.exists(self.path))

    def test_persistence_after_restart(self):
        self.run_cmd("SET a 1")
        self.run_cmd("HSET h f v")
        self.run_cmd("RPUSH l x")
        self.run_cmd("DEL a")
        db = self.reopen()
        self.assertIsNone(db.get("a"))
        self.assertEqual(db.hget("h", "f"), "v")
        self.assertEqual(db.lrange("l", 0, -1), ["x"])

    def test_torn_last_line_is_ignored(self):
        self.run_cmd("SET a 1")
        with open(self.path, "a", encoding="utf-8") as fh:
            fh.write('["SET","b"')  # simulated crash mid-write
        self.assertEqual(self.reopen().get("a"), "1")

    def test_del_exists(self):
        self.run_cmd("SET a 1")
        self.assertEqual(self.run_cmd("EXISTS a"), ["1"])
        self.assertEqual(self.run_cmd("DEL a"), ["1"])
        self.assertEqual(self.run_cmd("EXISTS a"), ["0"])
        self.assertEqual(self.run_cmd("DEL a"), ["0"])

    def test_mset_mget(self):
        self.run_cmd("MSET a 1 b 2")
        self.assertEqual(self.run_cmd("MGET b a z"), ["2", "1", ""])

    def test_ttl_and_lazy_expiry(self):
        self.run_cmd("SET a 1")
        self.assertEqual(self.run_cmd("TTL a"), ["-1"])
        self.assertEqual(self.run_cmd("EXPIRE a 10"), ["1"])
        self.assertEqual(self.run_cmd("TTL a"), ["10"])
        self.clock.now += 11
        self.assertEqual(self.run_cmd("GET a"), [""])
        self.assertEqual(self.run_cmd("TTL a"), ["-2"])
        self.assertEqual(self.run_cmd("EXPIRE missing 5"), ["0"])

    def test_range(self):
        self.run_cmd("MSET a 1 b 2 c 3 d 4 e 5")
        self.assertEqual(self.run_cmd("RANGE b d"), ["b", "c", "d", "END"])
        self.assertEqual(self.run_cmd('RANGE "" c'), ["a", "b", "c", "END"])
        self.assertEqual(self.run_cmd('RANGE d ""'), ["d", "e", "END"])

    def test_transaction_abort_and_commit(self):
        self.run_cmd("BEGIN")
        self.run_cmd("SET t 1")
        self.assertEqual(self.run_cmd("GET t"), ["1"])  # read-your-writes
        self.run_cmd("ABORT")
        self.assertEqual(self.run_cmd("GET t"), [""])
        self.run_cmd("BEGIN")
        self.run_cmd("SET t 2")
        self.assertIsNone(self.reopen().get("t"))  # not on disk before COMMIT
        self.run_cmd("COMMIT")
        self.assertEqual(self.reopen().get("t"), "2")
        self.assertTrue(self.run_cmd("COMMIT")[0].startswith("ERR"))

    def test_hashes(self):
        self.assertEqual(self.run_cmd("HSET h f1 v1"), ["1"])
        self.assertEqual(self.run_cmd("HSET h f1 v2"), ["0"])
        self.run_cmd("HSET h f2 v3")
        self.assertEqual(self.run_cmd("HGET h f1"), ["v2"])
        self.assertEqual(sorted(self.run_cmd("HGETALL h")), sorted(["f1", "v2", "f2", "v3"]))

    def test_lists(self):
        self.run_cmd("LPUSH l item1")
        self.run_cmd("RPUSH l item2")
        self.run_cmd("LPUSH l item3")
        self.assertEqual(self.run_cmd("LRANGE l 0 -1"), ["item3", "item1", "item2"])
        self.assertEqual(self.run_cmd("LRANGE l 1 1"), ["item1"])
        self.assertEqual(self.run_cmd("LPOP l"), ["item3"])
        self.assertEqual(self.run_cmd("RPOP l"), ["item2"])

    def test_incr_decr(self):
        self.assertEqual(self.run_cmd("INCR c"), ["1"])
        self.assertEqual(self.run_cmd("INCR c"), ["2"])
        self.assertEqual(self.run_cmd("DECR c"), ["1"])
        self.run_cmd("SET s abc")
        self.assertTrue(self.run_cmd("INCR s")[0].startswith("ERR"))

    def test_wrong_type_and_unknown(self):
        self.run_cmd("SET s abc")
        self.assertTrue(self.run_cmd("LPUSH s x")[0].startswith("ERR"))
        self.assertTrue(self.run_cmd("NOPE")[0].startswith("ERR"))

    def test_flushdb(self):
        self.run_cmd("MSET a 1 b 2")
        self.assertEqual(self.run_cmd("FLUSHDB"), ["OK"])
        self.assertEqual(self.run_cmd("GET a"), [""])
        self.assertEqual(self.reopen().get("b"), None)


class BlackBoxTest(unittest.TestCase):
    """Pipes commands into main.py like the Gradebot does."""

    def run_program(self, cwd: str, commands: str) -> list:
        proc = subprocess.run(
            [sys.executable, os.path.join(ROOT, "main.py")],
            input=commands,
            capture_output=True,
            text=True,
            cwd=cwd,
            timeout=10,
        )
        return proc.stdout.splitlines()

    def test_end_to_end_with_restart(self):
        with tempfile.TemporaryDirectory() as cwd:
            out = self.run_program(cwd, "SET name riket\nGET name\nEXIT\n")
            self.assertEqual(out, ["OK", "riket"])
            self.assertTrue(os.path.exists(os.path.join(cwd, "data.db")))
            self.assertEqual(self.run_program(cwd, "GET name\n"), ["riket"])


if __name__ == "__main__":
    unittest.main()
