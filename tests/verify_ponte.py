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
import json, os, sys, time
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
HERE = os.path.dirname(os.path.abspath(__file__))
def out(ev):
    print(json.dumps(ev), flush=True)
def note(name, obj):
    with open(os.path.join(HERE, name), "a") as f:
        f.write(json.dumps(obj) + "\n")
out({"type": "system", "subtype": "init", "session_id": sid,
     "model": model or "stub-default", "capabilities": ["interrupt_receipt_v1"]})
mode = os.environ.get("STUB_DENIALS", "empty")
TELEMETRY = {"subtype": "elicitation", "mcp_server_name": "blender-community",
    "message": "Blender MCP can collect data from this session to help improve the tool: "
               "your prompts, the code that gets generated, viewport screenshots, and scene "
               "metadata.\n\nThis is off by default and stays off unless you turn it on. Opt in?",
    "mode": "form", "requested_schema": {"type": "object", "properties": {"consent": {
        "type": "boolean", "default": False}}, "required": ["consent"]}}
UNKNOWN = {"subtype": "elicitation", "mcp_server_name": "supabase",
    "message": "This SQL includes destructive operations. Run it on project x?",
    "mode": "form", "requested_schema": {"type": "object", "properties": {}}}
n_elicit = 0
for line in sys.stdin:
    if not line.strip():
        continue
    msg = json.loads(line)
    t = msg.get("type")
    if t == "control_request" and (msg.get("request") or {}).get("subtype") == "interrupt":
        note("interrupts.jsonl", {"ts": time.time(), "request_id": msg.get("request_id")})
        out({"type": "control_response", "response": {"subtype": "success",
             "request_id": msg.get("request_id"), "response": {"still_queued": []}}})
        if os.environ.get("STUB_INTERRUPT_RESULT", "1") == "1":
            out({"type": "result", "subtype": "error_during_execution", "is_error": True,
                 "duration_ms": 5, "result": "interrupted", "session_id": sid,
                 "permission_denials": []})
        continue
    if t == "control_response":
        r = msg.get("response") or {}
        note("responses.jsonl", {"ts": time.time(), "request_id": r.get("request_id"),
                                 "response": r.get("response")})
        out({"type": "result", "subtype": "success", "is_error": False, "duration_ms": 5,
             "result": "elicitation:%s" % (r.get("response") or {}).get("action"),
             "session_id": sid, "permission_denials": []})
        continue
    if t != "user":
        continue
    elicit = os.environ.get("STUB_ELICIT")
    if elicit:
        n_elicit += 1
        out({"type": "control_request", "request_id": "el-%d" % n_elicit,
             "request": TELEMETRY if elicit == "telemetry" else UNKNOWN})
        continue
    if os.environ.get("STUB_HOLD") == "1":
        continue
    if os.environ.get("STUB_MCP_HANG") == "1":  # chamada MCP que nunca volta
        out({"type": "assistant", "message": {"content": [{"type": "tool_use",
             "id": "toolu_mcp1", "name": "mcp__blender-community__execute_blender_code",
             "input": {"code": "import time; time.sleep(200)"}}]}})
        continue
    slow = float(os.environ.get("STUB_SLOW", "0"))
    if slow:  # lento mas vivo: batimento a cada 0,3 s
        end = time.time() + slow
        while time.time() < end:
            out({"type": "tool_progress", "tool_use_id": "toolu_b", "elapsed_time_seconds": 1})
            time.sleep(0.3)
    if os.environ.get("STUB_REENTRY") == "1":  # F23: result, background, result
        out({"type": "system", "subtype": "task_started", "task_id": "bg1",
             "is_backgrounded": True, "description": "build"})
        out({"type": "result", "subtype": "success", "is_error": False, "duration_ms": 5,
             "result": "o build esta rodando, o Monitor me avisa", "session_id": sid,
             "total_cost_usd": 1.06, "permission_denials": []})
        time.sleep(float(os.environ.get("STUB_REENTRY_S", "1.5")))
        out({"type": "system", "subtype": "task_notification", "task_id": "bg1",
             "status": "completed"})
        out({"type": "assistant", "message": {"content": [{"type": "text", "text": "pronto"}]}})
        out({"type": "result", "subtype": "success", "is_error": False, "duration_ms": 5,
             "result": "build ok", "session_id": sid, "total_cost_usd": 3.08,
             "permission_denials": []})
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
               BRIDGE_WATCH_TICK_S="0.2")
    env.update(env_extra)
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


# ---------------- politica de elicitation (F01 / caos C1) ----------------
def stub_log(tmp, name):
    p = os.path.join(tmp, name)
    if not os.path.isfile(p):
        return []
    with open(p) as f:
        return [json.loads(l) for l in f if l.strip()]


def wait_for(fn, timeout, what):
    deadline = time.time() + timeout
    while time.time() < deadline:
        v = fn()
        if v:
            return v
        time.sleep(0.1)
    raise AssertionError("tempo esgotado (%ss): %s" % (timeout, what))


def all_events(port):
    return http(port, "/events?since=0&timeout=0")[1]["events"]


def alerts(port):
    return [e for e in all_events(port) if e.get("type") == "bridge_alert"]


def case_elicit_telemetry(port, tmp):
    t0 = time.time()
    code, r = http(port, "/send", {"text": "probe do blender"})
    assert code == 200, (code, r)
    resp = wait_for(lambda: stub_log(tmp, "responses.jsonl"), 5,
                    "resposta automatica a telemetria")
    dt = resp[0]["ts"] - t0
    assert dt <= 5, "decline levou %.2fs" % dt
    assert resp[0]["request_id"] == "el-1", resp
    assert resp[0]["response"] == {"action": "decline"}, resp
    st = wait_turn(port)
    assert st["last_turn"]["subtype"] == "success", st
    assert http(port, "/pending")[1]["pending"] == [], "pendente sobrou"
    assert st["pending"] == 0, st
    kinds = [a["kind"] for a in alerts(port)]
    assert "elicitation_auto" in kinds, kinds
    assert not stub_log(tmp, "interrupts.jsonl"), "telemetria nao encerra o turno"
    print("   decline em %.2fs" % dt)


def case_elicit_unknown(port, tmp):
    t0 = time.time()
    code, r = http(port, "/send", {"text": "sql destrutivo"})
    assert code == 200, (code, r)
    p = wait_for(lambda: http(port, "/pending")[1]["pending"], 3, "pedido em /pending")
    assert p[0]["request"]["mcp_server_name"] == "supabase", p
    resp = wait_for(lambda: stub_log(tmp, "responses.jsonl"), 8, "cancel no prazo")
    dt = resp[0]["ts"] - t0
    assert resp[0]["response"] == {"action": "cancel"}, resp
    assert 1.5 <= dt <= 6, "cancel fora da janela do prazo (2s): %.2fs" % dt
    intr = wait_for(lambda: stub_log(tmp, "interrupts.jsonl"), 3, "interrupt do turno")
    assert len(intr) == 1, intr
    assert http(port, "/pending")[1]["pending"] == [], "pendente sobrou"
    kinds = [a["kind"] for a in alerts(port)]
    for k in ("control_request_pendente", "control_request_expirado", "interrupt"):
        assert k in kinds, (k, kinds)
    print("   cancel + interrupt em %.2fs" % dt)


def case_elicit_muse_answers(port, tmp):
    code, r = http(port, "/send", {"text": "sql destrutivo autorizado"})
    assert code == 200, (code, r)
    p = wait_for(lambda: http(port, "/pending")[1]["pending"], 3, "pedido em /pending")
    code, r = http(port, "/respond", {"request_id": p[0]["request_id"],
                                       "response": {"action": "accept", "content": {}}})
    assert code == 200 and r["ok"], (code, r)
    resp = wait_for(lambda: stub_log(tmp, "responses.jsonl"), 3, "resposta da Muse")
    assert resp[0]["response"]["action"] == "accept", resp
    time.sleep(2.5)  # passa do prazo de 2s: nada mais pode acontecer
    assert len(stub_log(tmp, "responses.jsonl")) == 1, stub_log(tmp, "responses.jsonl")
    assert not stub_log(tmp, "interrupts.jsonl"), "nao podia interromper"
    assert http(port, "/pending")[1]["pending"] == []


# ---------------- vigia de turno (F02, F06, F13, F23) ----------------
FAST_WATCH = {"BRIDGE_SILENCE_WARN_S": "1", "BRIDGE_SILENCE_KILL_S": "2.5",
              "BRIDGE_CLOSE_GRACE_S": "1.5", "BRIDGE_ALERT_SCREENSHOT": "0"}


def wait_closed(port, timeout=10):
    def done():
        st = http(port, "/status")[1]
        return st if (not st["turn_in_flight"] and st.get("last_turn")
                      and st["last_turn"].get("outcome")) else None
    return wait_for(done, timeout, "turno fechado")


def case_c8_send_em_voo(port, tmp):
    code, r = http(port, "/send", {"text": "turno longo"})
    assert code == 200 and r["queued"] is False, (code, r)
    st = http(port, "/status")[1]
    assert st["turn_in_flight"] is True and st["turn"]["brief_id"] == r["brief_id"], st
    code, r2 = http(port, "/send", {"text": "atropelo"})
    assert code == 409 and "turno em andamento" in r2["error"], (code, r2)
    assert r2["turn_in_flight"] is True, r2
    code, r3 = http(port, "/send", {"text": "de proposito", "queue": True})
    assert code == 200 and r3["queued"] is True and r3["brief_id"] == r["brief_id"], (code, r3)
    assert http(port, "/status")[1]["turn"]["expected_results"] == 2
    code, r4 = http(port, "/interrupt", {"reason": "teste C8"})
    assert code == 200 and r4["ok"], (code, r4)
    st = wait_closed(port)
    assert st["last_turn"]["outcome"] == "interrompido", st["last_turn"]
    assert len(stub_log(tmp, "interrupts.jsonl")) == 1
    code, r5 = http(port, "/interrupt", {})
    assert code == 409, (code, r5)  # nada em voo
    code, r6 = http(port, "/send", {"text": "depois"})
    assert code == 200, (code, r6)


def case_c9_sem_result(port, tmp):
    t0 = time.time()
    code, r = http(port, "/send", {"text": "trava"})
    assert code == 200, (code, r)
    st = wait_closed(port, 12)
    dt = time.time() - t0
    t = st["last_turn"]
    assert t["outcome"] == "stalled", t
    assert t["cost_turn_usd"] is None and t["cost_reason"] == "sem result", t
    assert t["results"] == [], t
    assert 3.5 <= dt <= 8, "fechou fora do prazo (2,5 + 1,5 s): %.1fs" % dt
    assert len(stub_log(tmp, "interrupts.jsonl")) == 1
    kinds = [a["kind"] for a in alerts(port)]
    for k in ("silencio", "interrupt", "turno_fechado"):
        assert k in kinds, (k, kinds)
    code, r = http(port, "/send", {"text": "segue"})
    assert code == 200, "depois do stalled a ponte aceita turno novo"
    print("   stalled em %.1fs" % dt)


def case_lento_vivo(port, tmp):
    code, r = http(port, "/send", {"text": "build demorado"})
    assert code == 200, (code, r)
    st = wait_closed(port, 12)
    assert st["last_turn"]["outcome"] == "concluido", st["last_turn"]
    assert not stub_log(tmp, "interrupts.jsonl"), "lento com batimento nao e travado"
    assert "silencio" not in [a["kind"] for a in alerts(port)]


def case_mcp_longa(port, tmp):
    code, r = http(port, "/send", {"text": "probe"})
    assert code == 200, (code, r)
    def tool():
        st = http(port, "/status")[1]
        return st["tool_in_flight"] or None
    tf = wait_for(tool, 3, "ferramenta em voo no /status")
    assert tf[0]["name"].startswith("mcp__blender-community"), tf
    st = wait_closed(port, 10)
    assert st["last_turn"]["outcome"] == "mcp_longa", st["last_turn"]
    kinds = [a["kind"] for a in alerts(port)]
    assert "mcp_lenta" in kinds and "interrupt" in kinds, kinds


def case_teto(port, tmp):
    code, r = http(port, "/send", {"text": "turno sem fim"})
    assert code == 200, (code, r)
    st = wait_closed(port, 12)
    assert st["last_turn"]["outcome"] == "teto", st["last_turn"]
    intr = wait_for(lambda: stub_log(tmp, "interrupts.jsonl"), 8, "interrupt lido pelo stub")
    assert len(intr) == 1, intr


def case_reentrada(port, tmp):
    code, r = http(port, "/send", {"text": "build em background"})
    assert code == 200, (code, r)
    def waiting():
        st = http(port, "/status")[1]
        return st if (st["turn"] or {}).get("state") == "esperando_background" else None
    st = wait_for(waiting, 3, "brief esperando background")
    assert st["turn_in_flight"] is True and st["background_tasks"] == 1, st
    code, r2 = http(port, "/send", {"text": "acho que acabou"})
    assert code == 409, "result com background aberto nao libera o /send"
    st = wait_closed(port, 6)
    t = st["last_turn"]
    assert len(t["results"]) == 2 and t["outcome"] == "concluido", t
    assert abs(t["cost_turn_usd"] - 3.08) < 1e-6, t
    assert [round(x["cost_turn_usd"], 2) for x in t["results"]] == [1.06, 2.02], t
    assert len(http(port, "/turns")[1]["turns"]) == 1, "um brief, uma entrada"


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
        run_case("elicitation: telemetria blender -> decline em <= 5s (C1)", 8721,
                 {"STUB_EXP": iso(30), "STUB_ELICIT": "telemetry"},
                 case_elicit_telemetry),
        run_case("elicitation: desconhecida -> prazo, cancel e turno encerrado (D3/B)", 8722,
                 {"STUB_EXP": iso(30), "STUB_ELICIT": "unknown",
                  "BRIDGE_ELICIT_WAIT_S": "2"}, case_elicit_unknown),
        run_case("elicitation: Muse responde no prazo -> nada e cancelado", 8723,
                 {"STUB_EXP": iso(30), "STUB_ELICIT": "unknown",
                  "BRIDGE_ELICIT_WAIT_S": "2"}, case_elicit_muse_answers),
        run_case("vigia C8: /send em voo -> 409; queue:true entra; /interrupt", 8724,
                 {"STUB_EXP": iso(30), "STUB_HOLD": "1"}, case_c8_send_em_voo),
        run_case("vigia C9: claude sem result -> stalled, custo null 'sem result'", 8725,
                 dict(FAST_WATCH, STUB_EXP=iso(30), STUB_HOLD="1",
                      STUB_INTERRUPT_RESULT="0"), case_c9_sem_result),
        run_case("vigia: claude lento com batimento nao e interrompido", 8726,
                 dict(FAST_WATCH, STUB_EXP=iso(30), STUB_SLOW="4"), case_lento_vivo),
        run_case("vigia: chamada MCP passa do corte -> interrupt (U1)", 8727,
                 dict(FAST_WATCH, STUB_EXP=iso(30), STUB_MCP_HANG="1",
                      BRIDGE_SILENCE_WARN_S="60", BRIDGE_SILENCE_KILL_S="60",
                      BRIDGE_MCP_WARN_S="0.5", BRIDGE_MCP_KILL_S="1.5"), case_mcp_longa),
        run_case("vigia: teto duro por turno -> interrupt", 8728,
                 dict(FAST_WATCH, STUB_EXP=iso(30), STUB_SLOW="5",
                      BRIDGE_TURN_MAX_S="1.5"), case_teto),
        run_case("vigia F23: result com background aberto -> mesmo brief, custo em delta", 8729,
                 dict(FAST_WATCH, STUB_EXP=iso(30), STUB_REENTRY="1"), case_reentrada),
    ])
    sys.exit(0 if ok else 1)
