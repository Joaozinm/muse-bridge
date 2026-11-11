#!/usr/bin/env python3
"""Testes de unidade do log de eventos (F11, F12): leitura so do fim,
parciais fora do arquivo principal, rotacao que renomeia (nunca apaga),
trava de rotacao (D5) e lacuna no /events.

Uso:  python -m unittest tests.test_eventlog
"""
import hashlib
import json
import os
import sys
import tempfile
import unittest

os.environ["BRIDGE_DIR"] = tempfile.mkdtemp(prefix="ponte-unit-")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import server  # noqa: E402


def sha(path):
    with open(path, "rb") as f:
        return hashlib.sha256(f.read()).hexdigest()


class EventLogTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="ponte-log-")
        self.saved = (server.JSONL_PATH, server.PARTIAL_PATH, server.HOLD_PATH,
                      server.ROTATE_BYTES, server.MAX_EVENTS, server.next_seq)
        server.JSONL_PATH = os.path.join(self.dir, "events.jsonl")
        server.PARTIAL_PATH = os.path.join(self.dir, "events-partial.jsonl")
        server.HOLD_PATH = os.path.join(self.dir, "rotation-hold.json")
        server._file_size.clear()
        server._hold_logged.clear()
        with server.events_lock:
            del server.events[:]
        server.next_seq = 1

    def tearDown(self):
        (server.JSONL_PATH, server.PARTIAL_PATH, server.HOLD_PATH,
         server.ROTATE_BYTES, server.MAX_EVENTS, server.next_seq) = self.saved
        server._file_size.clear()
        with server.events_lock:
            del server.events[:]

    def write(self, path, seqs, kind="assistant", pad=0):
        with open(path, "a", encoding="utf-8") as f:
            for s in seqs:
                f.write(json.dumps({"type": kind, "seq": s, "x": "y" * pad}) + "\n")

    def test_tail_lines_atravessa_blocos(self):
        self.write(server.JSONL_PATH, range(1, 1001), pad=37)
        got = server.tail_lines(server.JSONL_PATH, 10, block=64)
        self.assertEqual([json.loads(l)["seq"] for l in got], list(range(991, 1001)))
        self.assertEqual(len(server.tail_lines(server.JSONL_PATH, 5000)), 1000)
        self.assertEqual(server.tail_lines(os.path.join(self.dir, "nada"), 5), [])

    def test_replay_junta_principal_e_parciais(self):
        server.MAX_EVENTS = 50
        main = [s for s in range(1, 201) if s % 3]
        part = [s for s in range(1, 201) if not s % 3]
        self.write(server.JSONL_PATH, main)
        self.write(server.PARTIAL_PATH, part, kind="stream_event")
        n = server.replay_jsonl()
        self.assertEqual(n, 50)
        self.assertEqual([e["seq"] for e in server.events], list(range(151, 201)))
        self.assertEqual(server.next_seq, 201)

    def test_parcial_vai_para_arquivo_proprio(self):
        server.push_event({"type": "stream_event", "event": {"type": "content_block_delta"}})
        server.push_event({"type": "assistant", "message": {}})
        with open(server.PARTIAL_PATH) as f:
            self.assertEqual([json.loads(l)["seq"] for l in f], [1])
        with open(server.JSONL_PATH) as f:
            self.assertEqual([json.loads(l)["seq"] for l in f], [2])
        self.assertEqual([e["seq"] for e in server.events], [1, 2], "memoria tem os dois")

    def test_rotacao_renomeia_sem_perder(self):
        server.ROTATE_BYTES = 10_000
        self.write(server.JSONL_PATH, range(1, 101), pad=100)  # ~13 KB
        before = sha(server.JSONL_PATH)
        server.next_seq = 101
        server.push_event({"type": "result"})
        rotated = [f for f in os.listdir(self.dir) if f.startswith("events-2")]
        self.assertEqual(len(rotated), 1, os.listdir(self.dir))
        with open(os.path.join(self.dir, rotated[0])) as f:
            seqs = [json.loads(l)["seq"] for l in f]
        self.assertEqual(seqs, list(range(1, 102)), "tudo, inclusive o evento novo")
        self.assertNotEqual(before, sha(os.path.join(self.dir, rotated[0])))
        self.assertFalse(os.path.exists(server.JSONL_PATH))
        server.push_event({"type": "result"})
        with open(server.JSONL_PATH) as f:
            self.assertEqual([json.loads(l)["seq"] for l in f], [102])

    def test_trava_suspende_a_rotacao(self):
        server.ROTATE_BYTES = 10_000
        self.write(server.JSONL_PATH, range(1, 101), pad=100)
        with open(server.HOLD_PATH, "w") as f:
            f.write("{}")
        server.next_seq = 101
        server.push_event({"type": "result"})
        self.assertEqual(sorted(os.listdir(self.dir)),
                         ["events.jsonl", "rotation-hold.json"])
        with open(server.JSONL_PATH) as f:
            self.assertEqual(len(f.readlines()), 101)

    def test_lacuna(self):
        server.MAX_EVENTS = 10
        for _ in range(30):
            server.push_event({"type": "assistant"})
        out, info = server.events_since(5)
        self.assertTrue(info["gap"])
        self.assertEqual(info["first_seq"], 21)
        self.assertEqual(len(out), 10)
        out, info = server.events_since(20)
        self.assertFalse(info["gap"])
        out, info = server.events_since(25)
        self.assertFalse(info["gap"])
        self.assertEqual([e["seq"] for e in out], list(range(26, 31)))


if __name__ == "__main__":
    unittest.main()
