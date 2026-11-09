#!/usr/bin/env python3
"""Verificacao minima: permissoes negadas por turno + checagem de login.

Sobe o server.py de verdade (porta e BRIDGE_DIR proprios, nao encosta no
servico em :8000) com um `claude` falso que controla o `auth status` e o
`permission_denials` do evento `result`. Uso:  python tests/verify_ponte.py
"""
import json
import os
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SERVER = os.path.join(ROOT, "server.py")

STUB = r'''
import json, os, sys
exp = os.environ.get("STUB_EXP") or None
if sys.argv[1:3] == ["auth", "status"]:
    d = {"loggedIn": os.environ.get("STUB_LOGGED", "1") == "1", "authMethod": "stub"}
    if exp:
        d["loginExpiresAt"] = exp
    print(json.dumps(d)); sys.exit(0)
print(json.dumps({"type": "system", "subtype": "init", "session_id": "stub"}), flush=True)
mode = os.environ.get("STUB_DENIALS", "empty")
for line in sys.stdin:
    if not line.strip():
        continue
    ev = {"type": "result", "subtype": "success", "is_error": False,
          "duration_ms": 5, "result": "ok", "session_id": "stub"}
    if mode == "list":
        ev["permission_denials"] = [{"tool_name": "Write", "tool_use_id": "toolu_x",
                                     "tool_input": {"file_path": "C:\\x.txt", "content": "x"}}]
    elif mode == "empty":
        ev["permission_denials"] = []
    if exp:
        ev["credential_expires_at"] = exp
    print(json.dumps(ev), flush=True)
'''


def iso(days):
    return (datetime.now(timezone.utc) + timedelta(days=days)).isoformat().replace("+00:00", "Z")


def http(port, path, body=None):
    url = "http://127.0.0.1:%d%s" % (port, path)
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method="POST" if data else "GET",
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read())


def run_case(name, port, env_extra, check):
    tmp = tempfile.mkdtemp(prefix="ponte-verify-")
    stub_py = os.path.join(tmp, "stub.py")
    with open(stub_py, "w", encoding="utf-8") as f:
        f.write(STUB)
    stub_cmd = os.path.join(tmp, "claude.cmd")
    with open(stub_cmd, "w", encoding="utf-8") as f:
        f.write('@"%s" "%s" %%*\n' % (sys.executable, stub_py))
    env = dict(os.environ, CLAUDE_EXE=stub_cmd, BRIDGE_DIR=tmp,
               BRIDGE_PORT=str(port), PYTHONUTF8="1", **env_extra)
    srv = subprocess.Popen([sys.executable, SERVER, tmp, "127.0.0.1"], env=env,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        for _ in range(50):
            try:
                if http(port, "/status")[0] == 200:
                    break
            except OSError:
                time.sleep(0.2)
        check(port)
        print("PASS", name)
        return True
    except AssertionError as e:
        print("FAIL", name, "-", e)
        return False
    finally:
        srv.terminate()
        srv.wait(timeout=10)


def wait_turn(port):
    for _ in range(50):
        st = http(port, "/status")[1]
        if st.get("last_turn"):
            return st
        time.sleep(0.2)
    raise AssertionError("turno nao concluiu")


def logs(port):
    return "".join(http(port, "/logs?lines=200")[1]["server"])


def case_denials(port):
    code, r = http(port, "/send", {"text": "oi"})
    assert code == 200 and r["ok"] and "login_warning" not in r, r
    st = wait_turn(port)
    t = st["last_turn"]
    assert t["denials_reported"] is True and t["denials_count"] == 1, t
    assert t["permission_denials"][0]["tool_name"] == "Write", t
    assert st["login"]["status"] == "ok", st["login"]
    assert http(port, "/turns")[1]["turns"][-1]["denials_count"] == 1
    lg = logs(port)
    assert "PERMISSOES NEGADAS NO TURNO: 1" in lg and "negacao 1/1: Write" in lg, lg


def case_absent_and_warning(port):
    code, r = http(port, "/send", {"text": "oi"})
    assert code == 200 and r["ok"] and "login_warning" in r, r
    st = wait_turn(port)
    t = st["last_turn"]
    assert t["denials_reported"] is False and t["permission_denials"] is None, t
    assert st["login"]["status"] == "warning", st["login"]
    lg = logs(port)
    assert "NAO REPORTADO" in lg and "AVISO LOGIN" in lg, lg


def case_expired(port):
    code, r = http(port, "/send", {"text": "oi"})
    assert code == 403 and r["error"] == "login expirado", (code, r)
    time.sleep(1)
    st = http(port, "/status")[1]
    assert st["last_turn"] is None and st["login"]["status"] == "expired", st
    lg = logs(port)
    assert "LOGIN EXPIRADO" in lg and "turno NAO iniciado" in lg, lg
    assert "turno iniciado" not in lg, lg


def case_logged_out(port):
    code, r = http(port, "/send", {"text": "oi"})
    assert code == 403 and r["error"] == "login ausente", (code, r)


def case_no_field(port):
    # CLI antigo (sem loginExpiresAt): estado desconhecido, turno NAO e bloqueado
    code, r = http(port, "/send", {"text": "oi"})
    assert code == 200 and r["ok"], (code, r)
    assert wait_turn(port)["login"]["status"] == "unknown"


if __name__ == "__main__":
    ok = all([
        run_case("negacoes listadas (log + /status + /turns)", 8711,
                 {"STUB_EXP": iso(30), "STUB_DENIALS": "list"}, case_denials),
        run_case("campo ausente -> 'nao reportado'; login <10d -> aviso, turno segue", 8712,
                 {"STUB_EXP": iso(3), "STUB_DENIALS": "absent"}, case_absent_and_warning),
        run_case("login expirado -> turno nao inicia", 8713,
                 {"STUB_EXP": iso(-1)}, case_expired),
        run_case("deslogado -> turno nao inicia", 8714,
                 {"STUB_EXP": iso(30), "STUB_LOGGED": "0"}, case_logged_out),
        run_case("sem loginExpiresAt -> desconhecido, nao bloqueia", 8715,
                 {"STUB_EXP": ""}, case_no_field),
    ])
    sys.exit(0 if ok else 1)
