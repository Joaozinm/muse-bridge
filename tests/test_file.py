#!/usr/bin/env python3
"""Testes do GET /file (sem subir a ponte: o Handler roda num servidor de teste
efemero em 127.0.0.1, porta aleatoria, so durante o teste).

Uso:  python -m unittest tests.test_file   (ou python tests/test_file.py)
"""
import json
import os
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from urllib.parse import quote

os.environ["BRIDGE_DIR"] = tempfile.mkdtemp(prefix="ponte-unit-")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import server  # noqa: E402

PNG = b"\x89PNG\r\n\x1a\n" + b"x" * 100


def make_repo():
    base = tempfile.mkdtemp(prefix="ponte-file-")
    repo = os.path.join(base, "repo")
    os.makedirs(os.path.join(repo, "gallery"))
    with open(os.path.join(repo, "gallery", "render.png"), "wb") as f:
        f.write(PNG)
    with open(os.path.join(repo, "notas.md"), "w", encoding="utf-8") as f:
        f.write("# ok\n")
    with open(os.path.join(repo, "segredo.env"), "w") as f:
        f.write("X=1\n")
    with open(os.path.join(repo, "server.py"), "w") as f:
        f.write("print(1)\n")
    with open(os.path.join(base, "fora.png"), "wb") as f:
        f.write(PNG)
    return base, repo


class ResolveTest(unittest.TestCase):
    def setUp(self):
        self.base, self.repo = make_repo()

    def code(self, rel):
        return server.resolve_repo_file(self.repo, rel)[0]

    def test_serve_imagem_e_texto(self):
        c, path, ctype = server.resolve_repo_file(self.repo, "gallery/render.png")
        self.assertEqual((c, ctype), (200, "image/png"))
        self.assertTrue(os.path.isfile(path))
        self.assertEqual(self.code("gallery\\render.png"), 200)
        self.assertEqual(self.code("notas.md"), 200)
        self.assertEqual(self.code("./gallery/../notas.md"), 200)  # '..' que fica dentro

    def test_fuga_e_absoluto_403(self):
        for rel in ["../fora.png", "gallery/../../fora.png", "..\\fora.png",
                    os.path.join(self.base, "fora.png"), "/etc/passwd.txt",
                    "\\fora.png", "\\\\host\\share\\a.png", ".", "gallery/.."]:
            self.assertEqual(self.code(rel), 403, rel)

    def test_invalido_400(self):
        for rel in ["", None, "C:fora.png", "notas.md:stream", "a\x00.png"]:
            self.assertEqual(self.code(rel), 400, repr(rel))

    def test_extensao_415(self):
        for rel in ["segredo.env", "server.py", "gallery", "gallery/render.PNG.exe"]:
            self.assertEqual(self.code(rel), 415, rel)

    def test_extensao_maiuscula_ok(self):
        os.rename(os.path.join(self.repo, "gallery", "render.png"),
                  os.path.join(self.repo, "gallery", "RENDER.PNG"))
        self.assertEqual(self.code("gallery/RENDER.PNG"), 200)

    def test_inexistente_404(self):
        self.assertEqual(self.code("gallery/nao-existe.png"), 404)

    def test_grande_413(self):
        big = os.path.join(self.repo, "big.log")
        with open(big, "wb") as f:
            f.seek(server.FILE_MAX_BYTES)
            f.write(b"\0")
        self.assertEqual(self.code("big.log"), 413)
        with open(big, "r+b") as f:
            f.truncate(server.FILE_MAX_BYTES)
        self.assertEqual(self.code("big.log"), 200)

    @unittest.skipUnless(sys.platform == "win32", "junction e so no Windows")
    def test_junction_para_fora_403(self):
        import _winapi
        _winapi.CreateJunction(self.base, os.path.join(self.repo, "atalho"))
        self.assertEqual(self.code("atalho/fora.png"), 403)


class HandlerTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.base, cls.repo = make_repo()
        cls._old = server.REPO_DIR
        server.REPO_DIR = cls.repo
        cls.srv = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
        cls.url = "http://127.0.0.1:%d" % cls.srv.server_address[1]
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()
        cls.srv.server_close()
        server.REPO_DIR = cls._old

    def get(self, rel):
        try:
            with urllib.request.urlopen(self.url + "/file?path=" + quote(rel), timeout=5) as r:
                return r.status, r.headers, r.read()
        except urllib.error.HTTPError as e:
            return e.code, e.headers, e.read()

    def test_baixa_png_inteiro(self):
        code, h, body = self.get("gallery/render.png")
        self.assertEqual(code, 200)
        self.assertEqual(h["Content-Type"], "image/png")
        self.assertEqual(int(h["Content-Length"]), len(PNG))
        self.assertEqual(body, PNG)

    def test_codigos_de_erro_em_json(self):
        for rel, want in [("../fora.png", 403), ("gallery/x.png", 404),
                          ("segredo.env", 415), ("", 400)]:
            code, h, body = self.get(rel)
            self.assertEqual(code, want, rel)
            self.assertFalse(json.loads(body)["ok"])

    def test_segue_o_repo_atual(self):
        outro = tempfile.mkdtemp(prefix="ponte-outro-")
        with open(os.path.join(outro, "so-aqui.txt"), "w") as f:
            f.write("oi")
        server.REPO_DIR = outro
        try:
            self.assertEqual(self.get("so-aqui.txt")[0], 200)
            self.assertEqual(self.get("gallery/render.png")[0], 404)
        finally:
            server.REPO_DIR = self.repo

    def test_rotas_antigas_intactas(self):
        with urllib.request.urlopen(self.url + "/transcript", timeout=5) as r:
            self.assertEqual(r.status, 200)
        try:
            urllib.request.urlopen(self.url + "/nao-existe", timeout=5)
        except urllib.error.HTTPError as e:
            self.assertEqual(e.code, 404)


if __name__ == "__main__":
    unittest.main()
