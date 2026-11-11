#!/usr/bin/env python3
"""Testes de unidade do custo em delta (F13) e do agrupamento por brief (F23).

Sequencia real da sessao 5db91d1f (events.jsonl, seq 1329801-1339493), um so
processo: 1,06 -> 3,08 -> 3,34 -> 3,57 -> 5,09 -> 7,46 -> 8,25.

Uso:  python -m unittest tests.test_turn
"""
import os
import sys
import tempfile
import unittest

os.environ["BRIDGE_DIR"] = tempfile.mkdtemp(prefix="ponte-unit-")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import server  # noqa: E402

REAL = [1.0552398, 3.0839741999999997, 3.3429508, 3.571046, 5.0906872,
        7.460678, 8.2480468]


class CostDeltaTest(unittest.TestCase):
    def test_sequencia_real_5db91d1f(self):
        prev, deltas = None, []
        for total in REAL:
            deltas.append(server.cost_delta(prev, total))
            prev = total
        self.assertEqual([round(d, 2) for d in deltas],
                         [1.06, 2.03, 0.26, 0.23, 1.52, 2.37, 0.79])
        self.assertAlmostEqual(sum(deltas), REAL[-1], places=6)

    def test_processo_novo_zera_o_contador(self):
        # sessao 9aeaf055: 19,51 (opus) -> troca para fable com --resume -> 4,04
        self.assertEqual(server.cost_delta(19.510488165, 4.042714805), 4.042715)

    def test_sem_total(self):
        for bad in (None, "1.0", True, [1]):
            self.assertIsNone(server.cost_delta(1.0, bad), bad)

    def test_primeiro_result(self):
        self.assertEqual(server.cost_delta(None, 1.5), 1.5)


class BriefGroupingTest(unittest.TestCase):
    """O brief 'Turno 2' (seq 1329802) gerou 4 results sem /send no meio."""

    def setUp(self):
        server.cur_turn = None
        server.last_brief = None
        server.turn_in_flight = False
        server.cost_base["last_total"] = 1.0552398
        server.bg_tasks.clear()
        server.tools_in_flight.clear()
        with server.turns_lock:
            del server.turns[:]
        self._alive = server.claude_alive
        server.claude_alive = lambda: True

    def tearDown(self):
        server.claude_alive = self._alive
        server.cur_turn = None
        server.last_brief = None
        server.turn_in_flight = False

    def result(self, seq, total):
        return server.on_result({"type": "result", "subtype": "success",
                                 "is_error": False, "total_cost_usd": total,
                                 "permission_denials": []}, seq)

    def test_reentrada_agrupa_no_mesmo_brief(self):
        server.open_turn(1329802, "claude-opus-5-5")
        server.on_stream_event({"type": "system", "subtype": "task_started",
                                "task_id": "t1", "is_backgrounded": True}, 1)
        self.result(1333060, REAL[1])
        self.assertTrue(server.turn_in_flight, "background aberto: brief continua")
        self.assertEqual(server.cur_turn["state"], "esperando_background")
        server.on_stream_event({"type": "system", "subtype": "task_notification",
                                "task_id": "t1", "status": "completed"}, 2)
        server.on_stream_event({"type": "assistant", "message": {"content": [
            {"type": "text", "text": "o build terminou"}]}}, 3)
        self.result(1333463, REAL[2])
        self.assertFalse(server.turn_in_flight)
        # reentrada depois de fechado: o CLI volta sozinho
        server.on_stream_event({"type": "assistant", "message": {"content": [
            {"type": "text", "text": "Monitor avisou"}]}}, 4)
        self.assertTrue(server.turn_in_flight, "reentrada reabre o brief")
        self.result(1333790, REAL[3])
        server.on_stream_event({"type": "assistant", "message": {"content": [
            {"type": "text", "text": "de novo"}]}}, 5)
        self.result(1336736, REAL[4])
        self.assertFalse(server.turn_in_flight)
        turns = server.turns_snapshot()
        self.assertEqual(len(turns), 1, "um brief, uma entrada em /turns")
        t = turns[0]
        self.assertEqual(len(t["results"]), 4)
        self.assertEqual(t["reentries"], 2)
        self.assertEqual(t["outcome"], "concluido")
        self.assertAlmostEqual(t["cost_turn_usd"], REAL[4] - REAL[0], places=6)
        self.assertEqual(t["cost_session_usd"], REAL[4])

    def test_ferramenta_em_voo(self):
        server.open_turn(10, None)
        server.on_stream_event({"type": "assistant", "message": {"content": [
            {"type": "tool_use", "id": "toolu_1",
             "name": "mcp__blender-community__execute_blender_code"}]}}, 11)
        st = server.turn_status()
        self.assertEqual(st["tool_in_flight"][0]["name"],
                         "mcp__blender-community__execute_blender_code")
        server.on_stream_event({"type": "user", "message": {"content": [
            {"type": "tool_result", "tool_use_id": "toolu_1"}]}}, 12)
        self.assertEqual(server.turn_status()["tool_in_flight"], [])


if __name__ == "__main__":
    unittest.main()
