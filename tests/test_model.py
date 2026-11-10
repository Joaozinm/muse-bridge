#!/usr/bin/env python3
"""Testes de unidade do modelo por turno (sem subir processo nenhum).

Uso:  python -m unittest tests.test_model   (ou python tests/test_model.py)
"""
import os
import sys
import tempfile
import unittest

os.environ["BRIDGE_DIR"] = tempfile.mkdtemp(prefix="ponte-unit-")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import server  # noqa: E402


class AllowlistTest(unittest.TestCase):
    def test_aceita_ids_e_apelidos(self):
        cases = {"claude-fable-5-2": "claude-fable-5-2",
                 "claude-opus-5-5": "claude-opus-5-5",
                 "fable": "claude-fable-5-2",
                 "opus": "claude-opus-5-5"}
        for value, want in cases.items():
            self.assertEqual(server.resolve_model(value), (want, None), value)

    def test_ausente_e_sem_model(self):
        self.assertEqual(server.resolve_model(None), (None, None))

    def test_rejeita_o_resto(self):
        for value in ["", "Fable", "OPUS", "opus ", "claude-sonnet-5-5",
                      "claude-fable-5-1", "gpt-4", "--model=x", "opus; rm -rf",
                      5, 0, True, ["opus"], {"id": "opus"}]:
            resolved, err = server.resolve_model(value)
            self.assertIsNone(resolved, value)
            self.assertIn("model invalido", err, value)


class RestartDecisionTest(unittest.TestCase):
    def test_so_reinicia_quando_muda(self):
        f = server.needs_model_restart
        self.assertFalse(f(None, None))
        self.assertFalse(f(None, "claude-fable-5-2"))
        self.assertFalse(f("claude-opus-5-5", "claude-opus-5-5"))
        self.assertTrue(f("claude-opus-5-5", None))
        self.assertTrue(f("claude-fable-5-2", "claude-opus-5-5"))
        self.assertTrue(f("claude-opus-5-5", "claude-fable-5-2"))


class ModelErrorTest(unittest.TestCase):
    def test_result_real_do_cli_2_1_319(self):
        ev = {"type": "result", "subtype": "success", "is_error": True,
              "api_error_status": 404,
              "result": "There's an issue with the selected model "
                        "(claude-x). It may not exist or you may not have access to it."}
        self.assertTrue(server.is_model_error(ev))

    def test_outros_resultados_nao_sao_erro_de_modelo(self):
        self.assertFalse(server.is_model_error({"is_error": False, "result": "ok"}))
        self.assertFalse(server.is_model_error(
            {"is_error": True, "api_error_status": 529, "result": "Overloaded"}))


class StatusTest(unittest.TestCase):
    def tearDown(self):
        server.current_model = None
        server.cli_model = None
        server.model_fallback = None

    def test_model_status(self):
        server.current_model, server.cli_model = None, "claude-opus-5-5"
        self.assertEqual(server.model_status()["model"], "claude-opus-5-5")
        server.current_model = "claude-fable-5-2"
        st = server.model_status()
        self.assertEqual(st["model"], "claude-fable-5-2")
        self.assertIsNone(st["model_fallback"])


if __name__ == "__main__":
    unittest.main()
