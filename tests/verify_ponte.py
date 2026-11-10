#!/usr/bin/env python3
"""Verificacao minima: permissoes negadas por turno, checagem de login e
modelo por turno.

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
argv = sys.argv[1:]
def opt(name):
    return argv[argv.index(name) + 1] if name in argv else None
model, resume = opt("--model"), opt("--resume")
with open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "args.jsonl"), "a") as f:
    f.write(json.dumps({"model": model, "resume": resume}) + "\n")
if model and model == os.environ.get("STUB_DIE_MODEL"):
    sys.exit(1)
sid = resume or "stub"
print(json.dumps({"type": "system", "subtype": "init", "session_id": sid,
                  "model": model or "stub-default"}), flush=True)
mode = os.environ.get("STUB_DENIALS", "empty")
for line in sys.stdin:
    if not line.strip():
        continue
    if os.environ.get("STUB_HOLD") == "1":
        continue
    ev = {"type": "result", "subtype": "success", "is_error": False,
          "duration_ms": 5, "result": "ok", "session_id": sid}
    if model and model == os.environ.get("STUB_ERR_MODEL"):
        ev.update(is_error=True, api_error_status=404,
                  result="There's an issue with the selected model (%s)." % model)
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
               BRIDGE_PORT=str(port), PYTHONUTF8="1", BRIDGE_MODEL_PROBE_S="1.5",
               **env_extra)
    srv = subprocess.Popen([sys.executable, SERVER, tmp, "127.0.0.1"], env=env,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        for _ in range(50):
            try:
                if http(port, "/status")[0] == 200:
                    break
            except OSError:
                time.sleep(0.2)
        check(port) if check.__code__.co_argcount == 1 else check(port, tmp)
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


# ---------------- modelo por turno ----------------
def spawns(tmp):
    with open(os.path.join(tmp, "args.jsonl")) as f:
        return [json.loads(l) for l in f if l.strip()]


def send_turn(port, body):
    """Manda um turno e espera o result dele."""
    before = (http(port, "/status")[1].get("last_turn") or {}).get("seq", 0)
    code, r = http(port, "/send", body)
    assert code == 200 and r["ok"], (code, r)
    for _ in range(50):
        st = http(port, "/status")[1]
        if (st.get("last_turn") or {}).get("seq", 0) > before:
            return r, st
        time.sleep(0.2)
    raise AssertionError("turno nao concluiu: %s" % body)


def case_model_switch(port, tmp):
    st = http(port, "/status")[1]
    assert st["model"] == "stub-default" and st["model_requested"] is None, st
    assert st["model_fallback"] is None, st
    send_turn(port, {"text": "sem model"})
    assert spawns(tmp) == [{"model": None, "resume": None}], spawns(tmp)
    r, st = send_turn(port, {"text": "planeja", "model": "fable"})
    assert r["model"] == "claude-fable-5-2", r
    assert spawns(tmp)[-1] == {"model": "claude-fable-5-2", "resume": "stub"}, spawns(tmp)
    assert st["model"] == "claude-fable-5-2" and st["session_id"] == "stub", st
    send_turn(port, {"text": "de novo", "model": "claude-fable-5-2"})
    send_turn(port, {"text": "sem model mantem"})
    assert len(spawns(tmp)) == 2, spawns(tmp)  # mesmo modelo: sem reinicio
    r, st = send_turn(port, {"text": "executa", "model": "opus"})
    assert spawns(tmp)[-1] == {"model": "claude-opus-5-5", "resume": "stub"}, spawns(tmp)
    assert st["model"] == "claude-opus-5-5" and st["model_fallback"] is None, st
    assert len(spawns(tmp)) == 3, spawns(tmp)


def case_model_invalid(port, tmp):
    for bad in ["gpt-4", "Fable", "claude-sonnet-5-5", "", 7, ["opus"], None]:
        body = {"text": "oi", "model": bad}
        code, r = http(port, "/send", body)
        if bad is None:  # null == ausente
            assert code == 200, (code, r)
            continue
        assert code == 400 and "model invalido" in r["error"], (bad, code, r)
    assert all(s["model"] is None for s in spawns(tmp)), spawns(tmp)


def case_model_die_fallback(port, tmp):
    r, st = send_turn(port, {"text": "planeja", "model": "fable"})
    assert st["model"] == "claude-opus-5-5", st
    fb = st["model_fallback"]
    assert fb["requested"] == "claude-fable-5-2" and fb["used"] == "claude-opus-5-5", fb
    assert "morreu" in fb["reason"], fb
    send_turn(port, {"text": "planeja 2", "model": "fable"})  # 2a falha
    n = len(spawns(tmp))
    r, st = send_turn(port, {"text": "planeja 3", "model": "fable"})
    assert len(spawns(tmp)) == n, "depois de 2 falhas nao tenta mais o Fable"
    assert st["model"] == "claude-opus-5-5" and st["alive"], st
    assert "FALLBACK DE MODELO" in logs(port)


def case_model_error_fallback(port, tmp):
    r, st = send_turn(port, {"text": "planeja", "model": "fable"})
    assert st["last_turn"]["is_error"] is True, st
    for _ in range(50):
        st = http(port, "/status")[1]
        if (st["model"] == "claude-opus-5-5" and st["alive"]
                and len(spawns(tmp)) == 3):
            break
        time.sleep(0.2)
    assert st["model"] == "claude-opus-5-5", st
    assert "turno falhou" in st["model_fallback"]["reason"], st
    assert spawns(tmp)[-1] == {"model": "claude-opus-5-5", "resume": "stub"}, spawns(tmp)


def case_model_busy(port, tmp):
    code, r = http(port, "/send", {"text": "turno longo"})
    assert code == 200, (code, r)
    code, r = http(port, "/send", {"text": "troca", "model": "fable"})
    assert code == 409 and "turno em andamento" in r["error"], (code, r)
    assert len(spawns(tmp)) == 1, spawns(tmp)


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
        run_case("model: troca so quando muda, com --resume", 8716,
                 {"STUB_EXP": iso(30)}, case_model_switch),
        run_case("model: fora da allowlist -> 400", 8717,
                 {"STUB_EXP": iso(30)}, case_model_invalid),
        run_case("model: processo morre -> Opus; 2 falhas -> nao tenta mais", 8718,
                 {"STUB_EXP": iso(30), "STUB_DIE_MODEL": "claude-fable-5-2"},
                 case_model_die_fallback),
        run_case("model: turno falha pelo modelo -> Opus", 8719,
                 {"STUB_EXP": iso(30), "STUB_ERR_MODEL": "claude-fable-5-2"},
                 case_model_error_fallback),
        run_case("model: troca durante turno -> 409", 8720,
                 {"STUB_EXP": iso(30), "STUB_HOLD": "1"}, case_model_busy),
    ])
    sys.exit(0 if ok else 1)
