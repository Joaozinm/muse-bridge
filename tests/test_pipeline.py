#!/usr/bin/env python3
"""Fase B, passo 9 (F04, F05): a ponte roda os mesmos gates do driver.

Integracao com um repo de mentira que tem driver/gates.py:
  - stub: confere os argumentos que a ponte passa ao `verify` e o /pipeline;
  - real: copia gates.py, stream_stats.py e runrecord.py da esteira (pula se a esteira
    nao estiver em ESTEIRA_DIR) e roda o fluxo inteiro: /send com orcamento -> eventos ->
    result -> fim do turno -> verify -> registro de run -> GET /pipeline.
Nenhum claude de verdade, nenhuma porta da ponte real (HTTP numa porta livre).

Uso:  python -m unittest tests.test_pipeline
"""
import json
import os
import shutil
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from unittest import mock

os.environ["BRIDGE_DIR"] = tempfile.mkdtemp(prefix="ponte-pipeline-")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import server  # noqa: E402

ESTEIRA = os.environ.get("ESTEIRA_DIR", r"C:\projects\esteira-jogos-fase0")

STUB = r'''
import json, sys, os
here = os.path.dirname(os.path.abspath(__file__))
if sys.argv[1] == "verify":
    open(os.path.join(here, "verify_args.json"), "w").write(json.dumps(sys.argv[2:]))
    print(json.dumps({"overall": "fail", "run": "runs/x.json", "stats": {"cost_turn_usd": 0.5},
                      "gates": [{"id": "B3", "estagio": "brief", "status": "fail", "detalhe": "sem orcamento"}]}))
    sys.exit(1)
if sys.argv[1] == "pipeline":
    print(json.dumps({"status": {"turno": {"estagio": "ship"}}, "runs": [{"projeto": "p"}]}))
'''


def reset_turn_state():
    server.cur_turn = None
    server.last_brief = None
    server.turn_in_flight = False
    server.cost_base["last_total"] = None
    server.bg_tasks.clear()
    server.tools_in_flight.clear()
    server.verify_state.update(last=None, running=0)
    with server.turns_lock:
        del server.turns[:]


class Base(unittest.TestCase):
    def setUp(self):
        reset_turn_state()
        self.repo = tempfile.mkdtemp(prefix="repo-mentira-")
        os.makedirs(os.path.join(self.repo, "driver"))
        self.p = [mock.patch.object(server, "REPO_DIR", self.repo),
                  mock.patch.object(server, "claude_alive", lambda: True),
                  mock.patch.object(server, "send_to_claude", lambda text: True),
                  mock.patch.object(server, "check_login", lambda *a, **k: {"status": "ok"}),
                  mock.patch.object(server, "needs_model_restart", lambda *a: False),
                  mock.patch.object(server, "bridge_alert", lambda *a, **k: None)]
        for x in self.p:
            x.start()
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        self.url = "http://127.0.0.1:%d" % self.httpd.server_address[1]

    def tearDown(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        for x in self.p:
            x.stop()
        reset_turn_state()
        shutil.rmtree(self.repo, ignore_errors=True)

    def http(self, method, path, body=None):
        req = urllib.request.Request(self.url + path, method=method,
                                     data=json.dumps(body).encode() if body is not None else None,
                                     headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                return r.status, json.loads(r.read())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read())

    def fake_turn(self, cost=0.42, long_call=False):
        """Eventos de um turno pelo caminho real da ponte (push_event + on_stream_event + on_result)."""
        def feed(ev):
            seq = server.push_event(ev)
            if ev["type"] == "result":
                server.on_result(ev, seq)
            else:
                server.on_stream_event(ev, seq)
        feed({"type": "system", "subtype": "init", "session_id": "s1", "model": "claude-opus-5-5"})
        feed({"type": "assistant", "message": {"content": [{"type": "tool_use", "id": "t1",
              "name": "mcp__blender-community__execute_blender_code", "input": {"code": "print(1)"}}]}})
        feed({"type": "stream_event", "event": {"type": "content_block_delta"}})  # parcial: fora da captura
        if long_call:
            time.sleep(0.3)
        feed({"type": "user", "message": {"content": [{"type": "tool_result", "tool_use_id": "t1", "content": "1"}]}})
        feed({"type": "result", "subtype": "success", "is_error": False, "duration_ms": 900,
              "total_cost_usd": cost, "session_id": "s1", "permission_denials": []})


class SendBudget(Base):
    def test_budget_invalido_400(self):
        for bad in ({"budget_min": 0}, {"budget_usd": -1}, {"budget_usd": True}, {"budget_min": "10"},
                    {"projeto": "../x"}, {"artefatos": "a.png"}):
            code, out = self.http("POST", "/send", {"text": "oi", **bad})
            self.assertEqual(code, 400, bad)
        self.assertFalse(server.turn_in_flight)

    def test_budget_aceito_e_guardado(self):
        code, out = self.http("POST", "/send", {"text": "BRIEF x", "budget_min": 10, "budget_usd": 2.5,
                                                 "projeto": "canario", "fase": "l1"})
        self.assertEqual(code, 200, out)
        self.assertEqual(out["budget"], {"budget_min": 10.0, "budget_usd": 2.5})
        b = server.cur_turn
        self.assertEqual((b["projeto"], b["fase"], b["repo"]), ("canario", "l1", self.repo))
        with open(b["brief_path"], encoding="utf-8") as f:
            self.assertEqual(f.read(), "BRIEF x")

    def test_teto_do_brief_interrompe(self):
        self.http("POST", "/send", {"text": "x", "budget_min": 1, "budget_usd": 1})
        called = []
        with mock.patch.object(server, "interrupt_turn", lambda reason: called.append(reason)):
            server.watch_turn(now=server.cur_turn["started_ts"] + 61)
        self.assertEqual(len(called), 1)
        self.assertIn("budget_min", called[0])
        self.assertEqual(server.cur_turn["interrupt_outcome"], "orcamento")


class StubRepo(Base):
    def setUp(self):
        super().setUp()
        with open(os.path.join(self.repo, "driver", "gates.py"), "w", encoding="utf-8") as f:
            f.write(STUB)
        os.makedirs(os.path.join(self.repo, "projects", "p", "checkpoints"))

    def test_verify_recebe_o_brief_e_publica(self):
        code, out = self.http("POST", "/send", {
            "text": "BRIEF", "budget_min": 10, "budget_usd": 2, "projeto": "p", "fase": "f",
            "checkpoint": "projects/p/checkpoints/f.md", "artefatos": ["projects/p/a.png", "../fora.png"]})
        self.assertEqual(code, 200)
        snap_path = server.cur_turn["stream_path"]
        self.fake_turn()
        for _ in range(100):
            if server.verify_state["last"]:
                break
            time.sleep(0.1)
        v = server.verify_state["last"]
        self.assertEqual(v["overall"], "fail")
        self.assertEqual(v["gates"][0]["id"], "B3")
        with open(os.path.join(self.repo, "driver", "verify_args.json"), encoding="utf-8") as f:
            args = json.load(f)
        opt = lambda k: args[args.index(k) + 1]  # noqa: E731
        self.assertEqual(opt("--stream"), snap_path)
        self.assertEqual((opt("--projeto"), opt("--fase"), opt("--caminho")), ("p", "f", "ponte"))
        self.assertEqual((float(opt("--budget-min")), float(opt("--budget-usd"))), (10.0, 2.0))
        self.assertAlmostEqual(float(opt("--cost-turn")), 0.42)
        self.assertIn("--record", args)
        self.assertTrue(opt("--checkpoint").endswith(os.path.join("projects", "p", "checkpoints", "f.md")))
        self.assertEqual(opt("--artifacts").count(","), 0)  # ../fora.png ficou de fora
        # o stream capturado tem os eventos do turno, sem o parcial
        with open(snap_path, encoding="utf-8") as f:
            types = [json.loads(x)["type"] for x in f]
        self.assertEqual(types, ["system", "assistant", "user", "result"])
        # /turns e /pipeline mostram o resultado
        last = server.turns_snapshot()[-1]
        self.assertEqual(last["verify"]["overall"], "fail")
        code, pipe = self.http("GET", "/pipeline")
        self.assertEqual(code, 200)
        self.assertTrue(pipe["available"])
        self.assertEqual(pipe["runs"], [{"projeto": "p"}])
        self.assertEqual(pipe["ponte"]["ultimo_verify"]["overall"], "fail")

    def test_repo_sem_gates(self):
        os.remove(os.path.join(self.repo, "driver", "gates.py"))
        code, pipe = self.http("GET", "/pipeline")
        self.assertFalse(pipe["available"])
        self.assertIn("gates.py", pipe["motivo"])
        self.http("POST", "/send", {"text": "x"})
        snap = server.turn_public(server.cur_turn)
        self.assertFalse(server.run_verify(snap)["available"])


@unittest.skipUnless(os.path.isfile(os.path.join(ESTEIRA, "driver", "gates.py")), "esteira ausente")
class RealGates(Base):
    """Os modulos de verdade da esteira dentro do repo de mentira."""

    def setUp(self):
        super().setUp()
        for m in ("gates.py", "stream_stats.py", "runrecord.py"):
            shutil.copy(os.path.join(ESTEIRA, "driver", m), os.path.join(self.repo, "driver", m))

    def wait_verify(self):
        for _ in range(200):
            if server.verify_state["last"]:
                return server.verify_state["last"]
            time.sleep(0.1)
        self.fail("verify nao terminou")

    def test_fluxo_inteiro_com_os_gates_da_esteira(self):
        brief = ("BRIEF: canario\nFEITO QUANDO: x\nPODE E NAO PODE: y\nDEVOLVA QUANDO: z\n"
                 "Formato do retorno: w\nArtefatos: `checkpoints/l1.md`\n")
        code, _ = self.http("POST", "/send", {"text": brief, "budget_min": 10, "budget_usd": 2,
                                              "projeto": "canario", "fase": "l1"})
        self.assertEqual(code, 200)
        self.fake_turn(cost=0.42)
        v = self.wait_verify()
        self.assertIsNone(v.get("error"), v)
        ids = {g["id"]: g["status"] for g in v["gates"]}
        self.assertEqual(ids["B1"], "pass")
        self.assertEqual(ids["B3"], "pass")
        self.assertEqual(ids["U1"], "pass")
        self.assertEqual(ids["V8"], "pass")
        self.assertEqual(v["overall"], "pass")
        self.assertAlmostEqual(v["stats"]["cost_turn_usd"], 0.42)
        self.assertEqual(v["stats"]["calls_mcp"], 1)
        with open(v["run"], encoding="utf-8") as f:
            rec = json.load(f)
        self.assertEqual((rec["caminho"], rec["projeto"], rec["fase"], rec["desfecho"]),
                         ("ponte", "canario", "l1", "concluido"))
        self.assertEqual(set(rec["estagios"]), {"brief", "build", "verify"})
        code, pipe = self.http("GET", "/pipeline")
        self.assertTrue(pipe["available"])
        self.assertEqual(pipe["runs"][0]["projeto"], "canario")
        self.assertEqual(pipe["status"]["turno"]["caminho"], "ponte")

    def test_sem_orcamento_b3_falha(self):
        self.http("POST", "/send", {"text": "BRIEF sem blocos"})
        self.fake_turn()
        v = self.wait_verify()
        ids = {g["id"]: g["status"] for g in v["gates"]}
        self.assertEqual(ids["B3"], "fail")
        self.assertEqual(ids["B1"], "fail")
        self.assertEqual(v["overall"], "fail")

    def test_pendencia_respondida_pela_ponte_nao_conta(self):
        self.http("POST", "/send", {"text": "x", "budget_min": 10, "budget_usd": 2})
        server.push_event({"type": "control_request", "request_id": "r1",
                           "request": {"subtype": "elicitation", "mcp_server_name": "blender-community"}})
        with mock.patch.object(server, "write_to_claude", lambda obj: True):
            server.respond_to_control("r1", {"action": "decline"})
        self.fake_turn()
        v = self.wait_verify()
        ids = {g["id"]: g["status"] for g in v["gates"]}
        self.assertEqual(ids["V8"], "pass")
        self.assertEqual(ids["U4"], "pass")


if __name__ == "__main__":
    unittest.main()
