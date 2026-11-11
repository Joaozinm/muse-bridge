#!/usr/bin/env python3
"""Testes de unidade da politica de elicitation (F01), com os pedidos reais
do events.jsonl (seq 1339774: telemetria do blender-community; seq 1203050:
confirmacao de SQL destrutivo do supabase).

Uso:  python -m unittest tests.test_policy
"""
import os
import sys
import tempfile
import unittest

os.environ["BRIDGE_DIR"] = tempfile.mkdtemp(prefix="ponte-unit-")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import server  # noqa: E402

TELEMETRIA = {
    "subtype": "elicitation", "mcp_server_name": "blender-community",
    "message": "Blender MCP can collect data from this session to help improve the "
               "tool: your prompts, the code that gets generated, viewport screenshots, "
               "and scene metadata. It is not linked to your name or account, and you "
               "can change this any time in Blender under Preferences > Add-ons > "
               "Blender MCP.\n\nThis is off by default and stays off unless you turn it "
               "on. Opt in?",
    "mode": "form",
    "requested_schema": {"type": "object", "properties": {"consent": {
        "type": "boolean", "title": "Contribute session data", "default": False}},
        "required": ["consent"]},
    "property_order": ["consent"]}

SUPABASE = {
    "subtype": "elicitation", "mcp_server_name": "supabase",
    "message": "This SQL includes destructive operations (DROP, DELETE, TRUNCATE or "
               "UPDATE without WHERE).\nIt may permanently remove data, tables, schemas "
               "or other objects.\nRun it on project vsgenvilaqufmqfcvaxe?",
    "mode": "form", "requested_schema": {"type": "object", "properties": {}},
    "property_order": []}


class PolicyMatchTest(unittest.TestCase):
    def test_telemetria_real_do_blender_e_recusada(self):
        rule = server.match_elicitation_policy(TELEMETRIA)
        self.assertIsNotNone(rule)
        self.assertEqual(rule["response"], {"action": "decline"})

    def test_supabase_destrutivo_nao_tem_regra(self):
        self.assertIsNone(server.match_elicitation_policy(SUPABASE))

    def test_mesmo_texto_em_outro_servidor_nao_casa(self):
        req = dict(TELEMETRIA, mcp_server_name="outro-mcp")
        self.assertIsNone(server.match_elicitation_policy(req))

    def test_blender_sem_campo_consent_nao_casa(self):
        req = dict(TELEMETRIA, requested_schema={"type": "object",
                                                 "properties": {"path": {}}})
        self.assertIsNone(server.match_elicitation_policy(req))

    def test_blender_outro_pedido_nao_casa(self):
        req = dict(TELEMETRIA, message="Overwrite file floresta.blend?")
        self.assertIsNone(server.match_elicitation_policy(req))

    def test_nao_elicitation_e_lixo(self):
        for req in [None, {}, "x", {"subtype": "can_use_tool"},
                    dict(TELEMETRIA, subtype="can_use_tool")]:
            self.assertIsNone(server.match_elicitation_policy(req), req)


class DefaultResponseTest(unittest.TestCase):
    def test_d3_cancel_para_elicitation(self):
        self.assertEqual(server.default_response(SUPABASE), {"action": "cancel"})

    def test_can_use_tool_nega(self):
        self.assertEqual(server.default_response({"subtype": "can_use_tool"})["behavior"],
                         "deny")

    def test_desconhecido_sem_resposta(self):
        self.assertIsNone(server.default_response({"subtype": "outra_coisa"}))
        self.assertIsNone(server.default_response(None))


if __name__ == "__main__":
    unittest.main()
