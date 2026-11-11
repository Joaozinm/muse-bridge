#!/usr/bin/env python3
"""Ponte Muse <-> Claude Code v2 (server.py) — versao enxuta.

Um unico processo `claude` fica vivo o tempo todo; a Muse manda mensagens
via HTTP e recebe os eventos do stream em tempo real. Sem banco de fila,
sem endpoints legados: o stream de eventos e a unica fonte de verdade.

Uso:  python server.py [REPO_DIR] [BIND_IP]
Env:  CLAUDE_EXE  (padrao: ~/.local/bin/claude.exe)
      BRIDGE_PORT (padrao: 8000)
      BRIDGE_DIR  (padrao: ~/.muse-bridge)
      PYTHONUTF8=1

Endpoints:
  GET  /health                       -> {"ok":true,"session":"alive"|"dead",...}
  GET  /status                       -> {"alive":...,"session_id":...,"pending":N,"repo":...,
                                        "model":...,"model_fallback":...,
                                        "turn_in_flight":..,"turn":{...},
                                        "last_event_age_s":..,"tool_in_flight":[...],
                                        "background_tasks":N,"limits":{...}}
  POST /send        {"text": "...", "model": "fable"|"opus"|<id>, "queue": bool,
                     "budget_min": N, "budget_usd": N,          (orcamento do brief; gate B3/U5)
                     "projeto": "...", "fase": "...",           (nome do registro de run)
                     "checkpoint": "<rel>", "artefatos": ["<rel>", ...]}  (gates V1/V6/V7)
                                     -> {"ok":true,"after_seq":N,"brief_id":N,"queued":..,"model":...,"budget":{...}}
                                     409 se ha turno em voo (salvo "queue": true)
                                     Ao fim do turno, se o repo atual tem driver/gates.py, a ponte roda
                                     `python driver/gates.py verify ... --record` (mesmos gates do driver)
  GET  /pipeline                     -> status.json + 10 ultimos runs do repo (driver/gates.py pipeline)
                                        + o ultimo verify da ponte; "available": false sem gates.py
  POST /interrupt   {"reason": "...", "force": bool} -> interrompe o turno em voo
                                     (409 sem turno em voo, salvo "force": true)
  GET  /events?since=N&timeout=S     -> {"events":[...]} (long-poll); com since
                                        anterior a janela: + "gap":true,"first_seq","last_seq"
  GET  /transcript                   -> pagina com a transcricao ao vivo
  GET  /pending                      -> {"pending":[...]} (control_requests sem resposta)
  POST /respond {"request_id": "...", "response": {...}} -> {"ok":true}
  GET  /screenshot                   -> PNG da tela principal
  GET  /logs?lines=N                 -> {"server": [...], "stderr": [...]}
  GET  /file?path=<relativo ao repo> -> o arquivo (renders e textos do repo atual).
                                        So png/jpg/jpeg/webp/gif/txt/md/json/log,
                                        ate 20 MB. 400 path ausente/invalido,
                                        403 fora da raiz do repo, 404 inexistente,
                                        413 grande demais, 415 extensao nao servida.
  GET  /turns                        -> {"turns":[...]} ultimos turnos concluidos,
                                        com as permissoes negadas de cada um
  POST /restart                      -> mata o claude e comeca sessao nova
  POST /switch-repo {"path": "..."}  -> troca o repo (reinicia a sessao com
                                        cwd novo). A esteira usa isso para
                                        atender varios projetos com uma so
                                        ponte, um de cada vez.

Como a Muse aprova coisas (o ponto central da v2):
  Quando o Claude Code (ou um servidor MCP, ex.: o do Supabase) precisa de
  uma decisao humana, ele emite um `control_request` no stream. O servidor
  guarda o pedido INTEIRO em /pending e o mostra na pagina /transcript.
  A Muse le o pedido, decide pela politica dela e responde via /respond —
  o servidor escreve o `control_response` no stdin do claude e o turno
  continua. Sem isso, o pedido fica sem resposta para sempre (foi o que
  travou as escritas no Supabase: a confirmacao do MCP nunca era respondida).

Seguranca: o servidor escuta SOMENTE no IP Tailscale passado em BIND_IP.
A rede privada (tailnet) e a fronteira de acesso — nao ha token nesta
versao, por decisao deliberada de simplicidade (um token criaria um
problema de bootstrap/segredo maior que o risco que ele fecha numa
tailnet de um unico usuario).
"""

import json
import os
import socket
import subprocess
import sys
import threading
import time
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

PORT = int(os.environ.get("BRIDGE_PORT", "8000"))
REPO_DIR = sys.argv[1] if len(sys.argv) > 1 else os.getcwd()
BIND_IP = sys.argv[2] if len(sys.argv) > 2 else "127.0.0.1"
CLAUDE_EXE = os.environ.get(
    "CLAUDE_EXE", os.path.expanduser("~/.local/bin/claude.exe"))
BRIDGE_DIR = os.environ.get(
    "BRIDGE_DIR", os.path.expanduser("~/.muse-bridge"))
LOG_PATH = os.path.join(BRIDGE_DIR, "sessao.log")
ERR_PATH = os.path.join(BRIDGE_DIR, "claude-stderr.log")
JSONL_PATH = os.path.join(BRIDGE_DIR, "events.jsonl")
SHOT_PATH = os.path.join(BRIDGE_DIR, "screenshot.png")
STATE_PATH = os.path.join(BRIDGE_DIR, "state.json")

os.makedirs(BRIDGE_DIR, exist_ok=True)

# Console do Windows nem sempre e UTF-8; nao deixar isso derrubar o servidor.
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

MAX_EVENTS = int(os.environ.get("BRIDGE_MAX_EVENTS", "5000"))


def log(msg):
    line = "[%s] %s" % (time.strftime("%Y-%m-%d %H:%M:%S"), msg)
    print(line, flush=True)
    try:
        with open(LOG_PATH, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except OSError:
        pass


# ---------------- buffer de eventos + log em disco ----------------
events = []
events_lock = threading.Lock()
next_seq = 1


# ---- log em disco (F11): parciais em arquivo proprio, rotacao por tamanho ----
# Os `stream_event` (--include-partial-messages) eram ~85% das linhas do
# events.jsonl. Eles continuam na memoria e no /events, mas no disco vao para
# events-partial.jsonl. Passando de ROTATE_BYTES, o arquivo e RENOMEADO
# (events-AAAAMMDD-HHMMSS.jsonl); nada e apagado. Com rotation-hold.json
# presente, a rotacao fica suspensa (decisao D5 do Joao sobre o arquivo antigo).
PARTIAL_PATH = os.path.join(BRIDGE_DIR, "events-partial.jsonl")
HOLD_PATH = os.path.join(BRIDGE_DIR, "rotation-hold.json")
ROTATE_BYTES = int(float(os.environ.get("BRIDGE_ROTATE_MB", "200")) * 1024 * 1024)
file_lock = threading.Lock()
_file_size = {}
_hold_logged = set()


def event_path(ev):
    return PARTIAL_PATH if ev.get("type") == "stream_event" else JSONL_PATH


def rotated_name(path, now=None):
    base, ext = os.path.splitext(path)
    stamp = time.strftime("%Y%m%d-%H%M%S", time.localtime(now or time.time()))
    cand = "%s-%s%s" % (base, stamp, ext)
    i = 1
    while os.path.exists(cand):
        i += 1
        cand = "%s-%s-%d%s" % (base, stamp, i, ext)
    return cand


def _maybe_rotate_locked(path):
    size = _file_size.get(path)
    if size is None:
        try:
            size = os.path.getsize(path)
        except OSError:
            size = 0
        _file_size[path] = size
    if size <= ROTATE_BYTES:
        return None
    if os.path.exists(HOLD_PATH):
        if path not in _hold_logged:
            _hold_logged.add(path)
            log("!!! %s tem %.0f MB (> %.0f MB) mas a rotacao esta suspensa por %s"
                % (os.path.basename(path), size / 1048576.0, ROTATE_BYTES / 1048576.0,
                   HOLD_PATH))
        return None
    dest = rotated_name(path)
    try:
        os.rename(path, dest)  # renomeia; nunca apaga
    except OSError as e:
        log("rotacao de %s falhou: %s" % (path, e))
        _file_size[path] = 0  # tenta de novo depois de mais ROTATE_BYTES
        return None
    _file_size[path] = 0
    return dest


def push_event(ev):
    """Guarda o evento na memoria e no disco (historico sobrevive a restart)."""
    global next_seq
    with events_lock:
        ev["seq"] = next_seq
        ev["ts"] = time.time()
        next_seq += 1
        events.append(ev)
        if len(events) > MAX_EVENTS:
            del events[:len(events) - MAX_EVENTS]
    line = (json.dumps(ev, ensure_ascii=False) + "\n").encode("utf-8")
    path = event_path(ev)
    rotated = None
    with file_lock:
        try:
            with open(path, "ab") as f:
                f.write(line)
            _file_size[path] = _file_size.get(path, 0) + len(line) \
                if path in _file_size else os.path.getsize(path)
            rotated = _maybe_rotate_locked(path)
        except OSError as e:
            log("JSONL falhou: %s" % e)
    if rotated:
        log("log de eventos rotacionado: %s -> %s" % (path, rotated))
    capture_turn_event(ev)
    return ev["seq"]


def tail_lines(path, n, block=1 << 20):
    """As ultimas n linhas do arquivo, lendo de tras para frente (nao le o
    arquivo inteiro: a partida com 930 MB levava 36 s). -> lista de bytes."""
    try:
        f = open(path, "rb")
    except OSError:
        return []
    with f:
        end = f.seek(0, 2)
        pos, buf = end, b""
        while pos > 0 and buf.count(b"\n") <= n:
            step = min(block, pos)
            pos -= step
            f.seek(pos)
            buf = f.read(step) + buf
        lines = buf.split(b"\n")
        if pos > 0:
            lines = lines[1:]  # primeira linha pode estar cortada
        return [l for l in lines if l.strip()][-n:]


def replay_jsonl():
    """Recarrega o fim do historico (principal + parciais) na memoria."""
    global next_seq
    loaded = []
    for path in (JSONL_PATH, PARTIAL_PATH):
        for raw in tail_lines(path, MAX_EVENTS):
            try:
                ev = json.loads(raw.decode("utf-8", errors="replace"))
            except ValueError:
                continue
            if isinstance(ev, dict) and isinstance(ev.get("seq"), int):
                loaded.append(ev)
    loaded.sort(key=lambda e: e["seq"])
    loaded = loaded[-MAX_EVENTS:]
    with events_lock:
        events.extend(loaded)
        for ev in loaded:
            next_seq = max(next_seq, ev["seq"] + 1)
    return len(loaded)


def events_since(since):
    """-> (eventos com seq > since, info de lacuna). Lacuna = o poller pediu a
    partir de um ponto que ja saiu da janela de MAX_EVENTS (F12)."""
    with events_lock:
        out = [e for e in events if e["seq"] > since]
        first = events[0]["seq"] if events else None
        last = next_seq - 1
    gap = first is not None and since + 1 < first
    return out, {"gap": gap, "first_seq": first, "last_seq": last}


def log_files_status():
    out = {}
    for name, path in (("events", JSONL_PATH), ("partial", PARTIAL_PATH)):
        try:
            out[name + "_mb"] = round(os.path.getsize(path) / 1048576.0, 1)
        except OSError:
            out[name + "_mb"] = 0
    out["rotate_mb"] = round(ROTATE_BYTES / 1048576.0, 1)
    out["rotation_hold"] = os.path.exists(HOLD_PATH)
    return out


# ---------------- control_requests pendentes ----------------
pending = {}
pending_deadline = {}  # request_id -> epoch em que a politica padrao (D3) age
pending_lock = threading.Lock()


def pending_snapshot():
    with pending_lock:
        return [dict(v) for v in pending.values()]


def bridge_alert(kind, level, message, **extra):
    """Alerta da propria ponte: vai para o stream (/events, /transcript) e o log.
    level: info (a ponte resolveu sozinha) | warn (atencao) | action (a ponte agiu
    sobre o turno: cancelou, interrompeu, encerrou)."""
    ev = {"type": "bridge_alert", "kind": kind, "level": level,
          "message": message}
    ev.update(extra)
    log("%s ALERTA [%s/%s] %s" % ("!!!" if level != "info" else "---",
                                   level, kind, message))
    return push_event(ev)


# ---------------- politica de elicitation (F01) ----------------
# Um servidor MCP pode pedir uma decisao humana (elicitation) e esperar sem
# prazo nenhum: foi isso que travou a v1 e a v2 da floresta (telemetria do
# blender-community, seq 1339774 e 1340425). A ponte responde sozinha o que a
# tabela conhece; o resto espera a Muse por ELICIT_WAIT_S e, sem resposta,
# e cancelado e o turno e encerrado (decisao D3, opcao B, do Joao).
ELICIT_WAIT_S = float(os.environ.get("BRIDGE_ELICIT_WAIT_S", "60"))

ELICITATION_POLICY = [
    {"id": "blender-telemetria",
     "server": "blender-community",
     # texto real do consent_prompt do mcp-for-blender 2.2.7 ("...collect data
     # from this session to help improve the tool... Opt in?")
     "message_any": ("collect data", "help improve", "opt in", "telemetry"),
     "schema_any": ("consent",),
     "response": {"action": "decline"},
     "why": "consentimento de telemetria: recusa decidida pelo Joao na v3"},
]


def match_elicitation_policy(request):
    """-> a regra da tabela que cobre este pedido, ou None. Pura (testavel).
    Exige servidor igual E texto de consentimento E (se a regra pede) o campo
    do formulario: nada de casar so pelo nome do servidor."""
    if not isinstance(request, dict) or request.get("subtype") != "elicitation":
        return None
    server = request.get("mcp_server_name")
    msg = str(request.get("message") or "").lower()
    schema = request.get("requested_schema") or {}
    props = schema.get("properties") if isinstance(schema, dict) else None
    props = props if isinstance(props, dict) else {}
    for rule in ELICITATION_POLICY:
        if server != rule["server"]:
            continue
        if not any(k in msg for k in rule["message_any"]):
            continue
        if rule.get("schema_any") and not any(k in props for k in rule["schema_any"]):
            continue
        return rule
    return None


def default_response(request):
    """Resposta da politica padrao (D3) para um pedido que ninguem respondeu.
    None = nao ha formato seguro conhecido; so o interrupt encerra o turno."""
    subtype = (request or {}).get("subtype")
    if subtype == "elicitation":
        return {"action": "cancel"}
    if subtype == "can_use_tool":
        return {"behavior": "deny",
                "message": "sem resposta em %.0fs; negado pela ponte" % ELICIT_WAIT_S}
    return None


def handle_control_request(ev, seq):
    """Chamado pelo reader para cada control_request. Nunca levanta."""
    rid = ev.get("request_id", "?")
    req = ev.get("request") or {}
    with pending_lock:
        pending[rid] = ev
    rule = match_elicitation_policy(req)
    if rule:
        ok = respond_to_control(rid, rule["response"])
        bridge_alert("elicitation_auto", "info",
                     "elicitation de %s respondida pela politica '%s': %s"
                     % (req.get("mcp_server_name"), rule["id"],
                        json.dumps(rule["response"])),
                     request_id=rid, rule=rule["id"], response=rule["response"],
                     ok=ok, request_seq=seq)
        if not ok:  # nao conseguiu escrever: cai no prazo padrao
            with pending_lock:
                pending_deadline[rid] = time.time() + ELICIT_WAIT_S
        return
    with pending_lock:
        pending_deadline[rid] = time.time() + ELICIT_WAIT_S
    log("control_request pendente: id=%s (seq=%s) — ver /pending; sem resposta "
        "em %.0fs a ponte cancela e encerra o turno" % (rid, seq, ELICIT_WAIT_S))
    bridge_alert("control_request_pendente", "warn",
                 "pedido %s de %s sem regra na politica; esperando a Muse por "
                 "%.0fs (POST /respond)" % (req.get("subtype"),
                                           req.get("mcp_server_name") or "claude",
                                           ELICIT_WAIT_S),
                 request_id=rid, request_seq=seq, wait_s=ELICIT_WAIT_S)


def control_cancelled(ev):
    """O CLI desistiu do pedido (control_cancel_request): sai do /pending."""
    rid = ev.get("request_id")
    with pending_lock:
        had = pending.pop(rid, None) is not None
        pending_deadline.pop(rid, None)
    if had:
        log("control_request cancelado pelo claude: id=%s" % rid)


def expire_pending(now=None):
    """Aplica a politica padrao (D3, opcao B) aos pedidos vencidos:
    responde cancel (ou deny) e encerra o turno com interrupt."""
    now = time.time() if now is None else now
    with pending_lock:
        due = [(rid, pending.get(rid)) for rid, dl in pending_deadline.items()
               if dl <= now]
        for rid, _ in due:
            pending_deadline.pop(rid, None)
    for rid, ev in due:
        if ev is None:
            continue
        req = ev.get("request") or {}
        resp = default_response(req)
        ok = respond_to_control(rid, resp) if resp is not None else False
        with pending_lock:
            pending.pop(rid, None)
        bridge_alert("control_request_expirado", "action",
                     "pedido %s de %s sem resposta em %.0fs: %s e turno encerrado"
                     % (req.get("subtype"), req.get("mcp_server_name") or "claude",
                        ELICIT_WAIT_S, json.dumps(resp) if resp else "sem resposta"),
                     request_id=rid, response=resp, ok=ok)
        why = "control_request %s sem resposta (D3: cancelar e encerrar)" % rid
        mark_interrupt("elicitation", why)
        interrupt_turn(why)


# ---------------- permissoes negadas por turno ----------------
# Em headless ninguem responde um pedido de permissao: o CLI nega a chamada e
# o turno pode terminar como "success" mesmo assim. O evento `result` traz
# `permission_denials` (lista; no CLI 2.1.319 cada item e
# {"tool_name","tool_use_id","tool_input"}). O formato do item nao e garantido,
# entao ele e tratado de forma generica e guardado inteiro.
MAX_TURNS = 20
turns = []
turns_lock = threading.Lock()


def summarize_denial(item):
    """Uma linha legivel por negacao, sem assumir o formato do item."""
    if isinstance(item, dict):
        name = item.get("tool_name") or item.get("tool") or "?"
        rest = {k: v for k, v in item.items() if k not in ("tool_name", "tool")}
        return "%s %s" % (name, json.dumps(rest, ensure_ascii=False)[:300])
    return json.dumps(item, ensure_ascii=False)[:300]


def record_turn(ev, seq):
    """Resumo de UM result e log das negacoes dele. O agrupamento por brief
    (varios results do mesmo /send) fica em on_result. Nunca levanta."""
    raw = ev.get("permission_denials")
    reported = isinstance(raw, list)
    denials = raw if reported else None
    turn = {"seq": seq, "ts": time.time(), "session_id": ev.get("session_id"),
            "subtype": ev.get("subtype"), "is_error": ev.get("is_error"),
            "denials_reported": reported, "permission_denials": denials,
            "denials_count": len(denials) if reported else None}
    if not reported:
        log("permissoes negadas: NAO REPORTADO pelo CLI (campo %s; seq=%s)"
            % ("ausente" if raw is None else "com formato inesperado: %s"
               % json.dumps(raw, ensure_ascii=False)[:200], seq))
    elif denials:
        log("!!! PERMISSOES NEGADAS NO TURNO: %d (seq=%s, status=%s) - ver /turns"
            % (len(denials), seq, ev.get("subtype")))
        for i, item in enumerate(denials, 1):
            log("!!!   negacao %d/%d: %s" % (i, len(denials), summarize_denial(item)))
    else:
        log("permissoes negadas: 0 (seq=%s)" % seq)
    return turn


# ---------------- turno em voo, vigia e custo (F02, F06, F13, F23) ----------------
# Um "turno" aqui e um BRIEF: tudo o que acontece entre um /send e o fim do
# trabalho que ele disparou, inclusive os `result` extras de quando o CLI
# reentra sozinho (tarefa em background terminou: F23, seq 1333463-1336736).
# Prazos (decisao D4 do Joao); env so para os testes.
SILENCE_WARN_S = float(os.environ.get("BRIDGE_SILENCE_WARN_S", "120"))
SILENCE_KILL_S = float(os.environ.get("BRIDGE_SILENCE_KILL_S", "300"))
TURN_MAX_S = float(os.environ.get("BRIDGE_TURN_MAX_S", str(45 * 60)))
MCP_WARN_S = float(os.environ.get("BRIDGE_MCP_WARN_S", "30"))
MCP_KILL_S = float(os.environ.get("BRIDGE_MCP_KILL_S", "120"))
CLOSE_GRACE_S = float(os.environ.get("BRIDGE_CLOSE_GRACE_S", "15"))
ALERT_SCREENSHOT = os.environ.get("BRIDGE_ALERT_SCREENSHOT", "1") == "1"

turn_lock = threading.RLock()
cur_turn = None          # brief em voo (dict) ou None
last_brief = None        # ultimo brief (aberto ou fechado), para reentradas
last_event_ts = None     # epoch do ultimo evento do claude (batimento)
tools_in_flight = {}     # tool_use_id -> {"name","ts","seq"}
bg_tasks = {}            # task_id -> {"description","ts"} (is_backgrounded)
_brief_n = [0]
# total_cost_usd do CLI e ACUMULADO POR PROCESSO (zera quando o processo sobe
# de novo, mesmo com --resume: 19,51 -> 4,04 na sessao 9aeaf055). A base zera
# em todo spawn.
cost_base = {"last_total": None}


def cost_delta(prev_total, total):
    """-> custo deste result. Pura. total menor que o anterior = contador zerou."""
    if not isinstance(total, (int, float)) or isinstance(total, bool):
        return None
    if prev_total is None or total < prev_total - 1e-9:
        return round(float(total), 6)
    return round(float(total) - prev_total, 6)


def _new_brief(brief_seq, model, queued_from=None):
    _brief_n[0] += 1
    now = time.time()
    return {"brief_id": _brief_n[0], "brief_seq": brief_seq,
            "started_ts": now, "model": model, "state": "running",
            "expected_results": 1, "results": [], "reentries": 0,
            "outcome": None, "reason": None, "closed_ts": None,
            "duration_s": None, "cost_turn_usd": None,
            "cost_session_usd": None, "cost_reason": None,
            "interrupt_ts": None, "interrupt_outcome": None,
            "warned": {}, "in_list": False,
            # Fase B, passo 9: orcamento, registro e gates da esteira
            "budget_min": None, "budget_usd": None, "projeto": None, "fase": None,
            "checkpoint": None, "artefatos": None, "repo": REPO_DIR,
            "stream_path": None, "brief_path": None, "verify": None,
            # campos do formato antigo de /turns (ultimo result)
            "seq": None, "ts": now, "session_id": session_id,
            "subtype": None, "is_error": None, "denials_reported": None,
            "permission_denials": None, "denials_count": None}


def _publish_locked(b):
    """Garante o brief em /turns (uma entrada por brief)."""
    if not b["in_list"]:
        with turns_lock:
            turns.append(b)
            del turns[:-MAX_TURNS]
        b["in_list"] = True


def open_turn(brief_seq, model, queued=False, meta=None, text=None):
    """Chamado pelo /send. Com queue=True e turno em voo, a mensagem entra no
    brief atual (mais um result esperado). -> o brief."""
    global cur_turn, last_brief, turn_in_flight, last_event_ts
    with turn_lock:
        if cur_turn is not None and queued:
            cur_turn["expected_results"] += 1
            return cur_turn
        b = _new_brief(brief_seq, model)
        b.update(meta or {})
        b["repo"] = REPO_DIR
        start_capture(b, text)
        cur_turn = last_brief = b
        turn_in_flight = True
        last_event_ts = time.time()
        return b


def _close_locked(b, outcome, reason, now=None):
    global cur_turn, turn_in_flight
    now = time.time() if now is None else now
    b.update(outcome=outcome, reason=reason, closed_ts=now, state="fechado",
             duration_s=round(now - b["started_ts"], 1))
    if not b["results"]:
        b["cost_turn_usd"] = None
        b["cost_reason"] = "sem result"
    _publish_locked(b)
    if cur_turn is b:
        cur_turn = None
        turn_in_flight = False
        tools_in_flight.clear()


def close_turn(outcome, reason, alert_level="action"):
    """Fecha o brief em voo (se houver) e avisa. -> o brief fechado ou None."""
    with turn_lock:
        b = cur_turn
        if b is None:
            return None
        _close_locked(b, outcome, reason)
        snap = turn_public(b)
    log("turno fechado: brief=%s desfecho=%s motivo=%s custo=%s"
        % (snap["brief_id"], outcome, reason, snap["cost_turn_usd"]))
    schedule_verify(snap)
    if alert_level:
        bridge_alert("turno_fechado", alert_level,
                     "turno %s fechado como %s: %s" % (snap["brief_id"], outcome, reason),
                     brief_id=snap["brief_id"], outcome=outcome,
                     cost_turn_usd=snap["cost_turn_usd"],
                     cost_reason=snap["cost_reason"])
    return snap


def mark_interrupt(outcome, reason):
    """Registra no brief em voo que a ponte pediu interrupt (o fechamento vem
    com o result, ou pelo vigia depois de CLOSE_GRACE_S)."""
    with turn_lock:
        b = cur_turn
        if b is not None and b["interrupt_ts"] is None:
            b["interrupt_ts"] = time.time()
            b["interrupt_outcome"] = outcome
            b["reason"] = reason


def on_stream_event(ev, seq):
    """Batimento, ferramentas em voo e tarefas em background. Nunca levanta."""
    global last_event_ts, cur_turn
    now = time.time()
    etype = ev.get("type")
    reopened = None
    with turn_lock:
        last_event_ts = now
        if etype == "assistant":
            content = (ev.get("message") or {}).get("content")
            for blk in content if isinstance(content, list) else []:
                if isinstance(blk, dict) and blk.get("type") == "tool_use":
                    tools_in_flight[blk.get("id")] = {
                        "name": blk.get("name"), "ts": now, "seq": seq}
            # F23: o CLI voltou a trabalhar sem /send -> o brief reabre
            if cur_turn is None and last_brief is not None:
                b = last_brief
                b.update(state="running", outcome=None, closed_ts=None,
                         interrupt_ts=None, interrupt_outcome=None)
                b["reentries"] += 1
                b["expected_results"] = len(b["results"]) + 1
                cur_turn = b
                globals()["turn_in_flight"] = True
                reopened = b["brief_id"]
        elif etype == "user":
            content = (ev.get("message") or {}).get("content")
            for blk in content if isinstance(content, list) else []:
                if isinstance(blk, dict) and blk.get("type") == "tool_result":
                    tools_in_flight.pop(blk.get("tool_use_id"), None)
        elif etype == "system":
            st = ev.get("subtype")
            if st == "task_started" and ev.get("is_backgrounded"):
                bg_tasks[ev.get("task_id")] = {
                    "description": ev.get("description"), "ts": now}
            elif st == "task_notification":
                bg_tasks.pop(ev.get("task_id"), None)
                b = cur_turn
                if (b is not None and b["state"] == "esperando_background"
                        and not bg_tasks):
                    b["state"] = "running"
    if reopened is not None:
        log("turno %s reaberto: o claude voltou a trabalhar sem /send (reentrada)"
            % reopened)


def on_result(ev, seq):
    """Agrupa o result no brief, calcula o custo em delta e decide se o
    brief acabou (F13, F23). Nunca levanta (o reader protege)."""
    summary = record_turn(ev, seq)
    total = ev.get("total_cost_usd")
    with turn_lock:
        delta = cost_delta(cost_base["last_total"], total)
        if isinstance(total, (int, float)):
            cost_base["last_total"] = float(total)
        b = cur_turn
        if b is None:  # result sem brief aberto (ex.: ponte reiniciou no meio)
            b = last_brief if (last_brief is not None and last_brief["closed_ts"]
                               and time.time() - last_brief["closed_ts"] < 5) else None
            if b is None:
                b = _new_brief(None, current_model)
                b["reason"] = "result sem /send"
        tools_in_flight.clear()
        b["results"].append({"seq": seq, "subtype": ev.get("subtype"),
                             "is_error": ev.get("is_error"),
                             "duration_ms": ev.get("duration_ms"),
                             "cost_turn_usd": delta,
                             "cost_session_usd": total})
        costs = [r["cost_turn_usd"] for r in b["results"]
                 if r["cost_turn_usd"] is not None]
        b["cost_turn_usd"] = round(sum(costs), 6) if costs else None
        b["cost_reason"] = None if costs else "result sem total_cost_usd"
        b["cost_session_usd"] = total
        prev_d = b["permission_denials"] if b["results"][:-1] else []
        if summary["denials_reported"] and (prev_d is not None):
            denials = list(prev_d or []) + list(summary["permission_denials"])
        else:
            denials = None
        b.update(seq=seq, ts=summary["ts"], session_id=summary["session_id"],
                 subtype=summary["subtype"], is_error=summary["is_error"],
                 denials_reported=denials is not None,
                 permission_denials=denials,
                 denials_count=len(denials) if denials is not None else None)
        _publish_locked(b)
        decision = None
        if b is cur_turn:
            if b["interrupt_ts"] is not None:
                decision = (b["interrupt_outcome"], b["reason"])
            elif len(b["results"]) >= b["expected_results"]:
                if bg_tasks:
                    b["state"] = "esperando_background"
                else:
                    decision = ("erro" if ev.get("is_error") else "concluido",
                                "result %s" % ev.get("subtype"))
        elif b["closed_ts"] is None:  # brief novo, sem /send: so registra
            _close_locked(b, "erro" if ev.get("is_error") else "concluido",
                          "result sem /send")
    log("custo: turno=%s sessao=%s (seq=%s)" % (delta, total, seq))
    if decision:
        close_turn(decision[0], decision[1],
                   alert_level=None if decision[0] in ("concluido", "erro") else "action")
    return b


def turn_public(b):
    if b is None:
        return None
    out = {k: v for k, v in b.items() if k not in ("warned", "in_list")}
    out["results"] = [dict(r) for r in b["results"]]
    return out


def turn_status(now=None):
    """O que o /status mostra do turno em voo."""
    now = time.time() if now is None else now
    with turn_lock:
        b = cur_turn
        tools = [{"tool_use_id": k, "name": v["name"],
                  "age_s": round(now - v["ts"], 1)}
                 for k, v in tools_in_flight.items()]
        out = {"turn_in_flight": b is not None,
               "last_event_ts": last_event_ts,
               "last_event_age_s": (round(now - last_event_ts, 1)
                                    if last_event_ts else None),
               "tool_in_flight": tools,
               "background_tasks": len(bg_tasks),
               "turn": None}
        if b is not None:
            t = turn_public(b)
            t["age_s"] = round(now - b["started_ts"], 1)
            out["turn"] = t
        out["limits"] = {"silence_warn_s": SILENCE_WARN_S,
                         "silence_kill_s": SILENCE_KILL_S,
                         "turn_max_s": TURN_MAX_S, "mcp_warn_s": MCP_WARN_S,
                         "mcp_kill_s": MCP_KILL_S,
                         "elicit_wait_s": ELICIT_WAIT_S}
        return out


def reset_turn_on_spawn():
    """Processo novo: o brief em voo (se houver) morreu com o anterior."""
    global last_event_ts
    if cur_turn is not None:
        close_turn("interrompido", "sessao do claude reiniciada")
    with turn_lock:
        tools_in_flight.clear()
        bg_tasks.clear()
        cost_base["last_total"] = None
        last_event_ts = time.time()


def watch_turn(now=None):
    """Um tique do vigia sobre o brief em voo. Decide fora do lock e age depois."""
    now = time.time() if now is None else now
    warn, kill, close = [], None, None
    with turn_lock:
        b = cur_turn
        if b is None:
            return
        if not claude_alive():
            close = ("morto", "processo do claude morreu com o turno em voo")
        elif b["interrupt_ts"] is not None:
            if now - b["interrupt_ts"] >= CLOSE_GRACE_S:
                close = (b["interrupt_outcome"],
                         "%s; sem result %.0fs depois do interrupt"
                         % (b["reason"], CLOSE_GRACE_S))
        else:
            age = now - b["started_ts"]
            silent = now - (last_event_ts or b["started_ts"])
            bmin = b.get("budget_min")
            if bmin and age >= bmin * 60 and age < TURN_MAX_S:
                kill = ("orcamento", "passou do teto de %.0f min do brief (budget_min)" % bmin)
            elif age >= TURN_MAX_S:
                kill = ("teto", "teto duro de %.0f min por turno" % (TURN_MAX_S / 60))
            elif b["state"] == "running" and silent >= SILENCE_KILL_S:
                kill = ("stalled", "%.0fs sem nenhum evento" % silent)
            elif b["state"] == "running":
                if silent >= SILENCE_WARN_S and b["warned"].get("silence") != last_event_ts:
                    b["warned"]["silence"] = last_event_ts
                    warn.append(("silencio", "%.0fs sem evento com turno em voo "
                                 "(interrupt em %.0fs)" % (silent, SILENCE_KILL_S),
                                 {"silent_s": round(silent, 1)}))
                for tid, t in list(tools_in_flight.items()):
                    if not str(t["name"] or "").startswith("mcp__"):
                        continue
                    tage = now - t["ts"]
                    if tage >= MCP_KILL_S:
                        kill = ("mcp_longa", "chamada %s passou de %.0fs"
                                % (t["name"], MCP_KILL_S))
                        break
                    if tage >= MCP_WARN_S and not b["warned"].get(tid):
                        b["warned"][tid] = True
                        warn.append(("mcp_lenta", "chamada %s em voo ha %.0fs (corte em %.0fs)"
                                     % (t["name"], tage, MCP_KILL_S),
                                     {"tool": t["name"], "tool_use_id": tid,
                                      "age_s": round(tage, 1)}))
    for kind, msg, extra in warn:
        if kind == "silencio":
            extra["pending"] = len(pending_snapshot())
            if ALERT_SCREENSHOT:
                extra["screenshot"] = "/screenshot" if take_screenshot() else None
        bridge_alert(kind, "warn", msg, **extra)
    if close:
        close_turn(close[0], close[1])
    elif kill:
        mark_interrupt(kill[0], kill[1])
        interrupt_turn(kill[1])


# ---------------- gates da esteira no caminho da ponte (F04, F05) ----------------
# Fase B, passo 9 da auditoria 2026-11-11: os mesmos gates do driver valem aqui. A
# ponte grava o stream de cada brief em BRIDGE_DIR/turnos/ (o events.jsonl nao muda)
# e, no fim do turno, roda `python driver/gates.py verify` do repo atual, se existir.
TURNS_DIR = os.path.join(BRIDGE_DIR, "turnos")
GATES_REL = os.path.join("driver", "gates.py")
VERIFY_TIMEOUT_S = float(os.environ.get("BRIDGE_VERIFY_TIMEOUT_S", "180"))
PIPELINE_TIMEOUT_S = float(os.environ.get("BRIDGE_PIPELINE_TIMEOUT_S", "20"))
SUB_NO_WINDOW = 0x08000000 if os.name == "nt" else 0
capture_lock = threading.Lock()
verify_state = {"last": None, "running": 0}
_SAFE_NAME = __import__("re").compile(r"^[A-Za-z0-9_.-]{1,60}$")


def parse_send_meta(data):
    """Campos opcionais do /send. -> (meta, erro)."""
    meta = {}
    for k in ("budget_min", "budget_usd"):
        v = data.get(k)
        if v is None:
            continue
        if isinstance(v, bool) or not isinstance(v, (int, float)) or v <= 0:
            return None, "%s precisa ser numero > 0" % k
        meta[k] = float(v)
    for k in ("projeto", "fase"):
        v = data.get(k)
        if v is None:
            continue
        if not isinstance(v, str) or not _SAFE_NAME.match(v):
            return None, "%s invalido (letras, numeros, _ . -; ate 60)" % k
        meta[k] = v
    ck = data.get("checkpoint")
    if ck is not None:
        if not isinstance(ck, str) or not ck:
            return None, "checkpoint precisa ser caminho relativo ao repo"
        meta["checkpoint"] = ck
    arts = data.get("artefatos")
    if arts is not None:
        if not isinstance(arts, list) or not all(isinstance(a, str) and a for a in arts):
            return None, "artefatos precisa ser lista de caminhos relativos ao repo"
        meta["artefatos"] = arts
    return meta, None


def start_capture(b, text):
    """Abre os arquivos do brief: texto do /send e stream (jsonl) dos eventos."""
    try:
        os.makedirs(TURNS_DIR, exist_ok=True)
        base = os.path.join(TURNS_DIR, "%s-brief%d" % (
            datetime.fromtimestamp(b["started_ts"]).strftime("%Y%m%d-%H%M%S"), b["brief_id"]))
        b["brief_path"] = base + ".md"
        b["stream_path"] = base + ".jsonl"
        with open(b["brief_path"], "w", encoding="utf-8") as f:
            f.write(text or "")
        open(b["stream_path"], "w", encoding="utf-8").close()
    except OSError as e:
        log("captura do brief falhou: %s" % e)
        b["stream_path"] = b["brief_path"] = None


def capture_turn_event(ev):
    """Copia o evento para o stream do brief em voo (sem os parciais). Nunca levanta."""
    if ev.get("type") == "stream_event":
        return
    b = cur_turn
    path = b.get("stream_path") if b is not None else None
    if not path:
        return
    try:
        line = json.dumps(ev, ensure_ascii=False) + "\n"
        with capture_lock:
            with open(path, "a", encoding="utf-8") as f:
                f.write(line)
    except (OSError, TypeError, ValueError) as e:
        log("captura do evento falhou: %s" % e)


def gates_script(repo):
    path = os.path.join(repo or "", GATES_REL)
    return path if repo and os.path.isfile(path) else None


def _inside(repo, rel):
    target = os.path.abspath(os.path.join(repo, rel))
    try:
        return target if os.path.commonpath([os.path.abspath(repo), target]) == os.path.abspath(repo) else None
    except ValueError:
        return None


def build_verify_cmd(snap, script, pending_now=0):
    repo = snap["repo"]
    cmd = [sys.executable, script, "verify", "--stream", snap["stream_path"],
           "--pending", str(pending_now), "--turn-start", repr(snap["started_ts"]),
           "--record", "--caminho", "ponte",
           "--projeto", snap.get("projeto") or "ponte",
           "--fase", snap.get("fase") or "brief%d" % snap["brief_id"]]
    if snap.get("brief_path"):
        cmd += ["--brief", snap["brief_path"]]
    if snap.get("model"):
        cmd += ["--modelo", snap["model"]]
    for k, flag in (("budget_min", "--budget-min"), ("budget_usd", "--budget-usd"),
                    ("cost_turn_usd", "--cost-turn")):
        if snap.get(k) is not None:
            cmd += [flag, repr(snap[k])]
    if snap.get("checkpoint"):
        ck = _inside(repo, snap["checkpoint"])
        if ck:
            cmd += ["--checkpoint", ck]
    arts = [a for a in (_inside(repo, r) for r in snap.get("artefatos") or []) if a]
    if arts:
        cmd += ["--artifacts", ",".join(arts)]
    return cmd


def _store_verify(brief_id, result):
    with turn_lock:
        with turns_lock:
            known = list(turns)
        for b in [cur_turn, last_brief] + known:
            if b is not None and b.get("brief_id") == brief_id:
                b["verify"] = result
                break
    verify_state["last"] = result


def run_verify(snap):
    """Roda `driver/gates.py verify` do repo do brief. -> resultado publico (dict). Nunca levanta."""
    script = gates_script(snap.get("repo"))
    base = {"brief_id": snap["brief_id"], "repo": snap.get("repo"), "ts": time.time()}
    if not script:
        return dict(base, available=False, motivo="repo sem %s" % GATES_REL)
    if not snap.get("stream_path") or not os.path.exists(snap["stream_path"]):
        return dict(base, available=True, overall=None, error="stream do brief ausente")
    cmd = build_verify_cmd(snap, script, len(pending_snapshot()))
    t0 = time.time()
    try:
        p = subprocess.run(cmd, cwd=snap["repo"], capture_output=True, text=True, encoding="utf-8",
                           errors="replace", timeout=VERIFY_TIMEOUT_S, creationflags=SUB_NO_WINDOW,
                           env=dict(os.environ, PYTHONUTF8="1", PYTHONIOENCODING="utf-8"))
        lines = [ln for ln in p.stdout.splitlines() if ln.strip().startswith("{")]
        data = json.loads(lines[-1]) if lines else {"error": "verify sem JSON (rc=%s): %s"
                                                       % (p.returncode, p.stderr[-400:])}
    except (OSError, subprocess.TimeoutExpired, ValueError) as e:
        data = {"error": "verify falhou: %s" % e}
    out = dict(base, available=True, seconds=round(time.time() - t0, 1),
               overall=data.get("overall"), gates=data.get("gates"),
               run=data.get("run"), error=data.get("error"),
               stats={k: (data.get("stats") or {}).get(k) for k in (
                   "duration_s", "cost_turn_usd", "calls_total", "calls_mcp", "max_mcp_call_s",
                   "max_out_chars", "pending_end", "permission_denials")})
    return out


def _verify_thread(snap):
    verify_state["running"] += 1
    try:
        res = run_verify(snap)
        _store_verify(snap["brief_id"], res)
        if not res.get("available"):
            return
        reds = [g for g in res.get("gates") or [] if g.get("status") == "fail"]
        log("verify do brief %s: %s (%d gate(s) vermelho(s)); run=%s"
            % (snap["brief_id"], res.get("overall") or res.get("error"), len(reds), res.get("run")))
        if res.get("error") or reds:
            bridge_alert("gates", "warn",
                         "brief %s: %s" % (snap["brief_id"], res.get("error") or "; ".join(
                             "%s: %s" % (g.get("id"), g.get("detalhe")) for g in reds)[:600]),
                         brief_id=snap["brief_id"], overall=res.get("overall"), run=res.get("run"))
    except Exception as e:  # o verify nunca derruba a ponte
        log("verify do brief %s quebrou: %s" % (snap.get("brief_id"), e))
    finally:
        verify_state["running"] -= 1


def schedule_verify(snap, wait=False):
    """Dispara o verify em segundo plano (o reader nao espera um subprocesso)."""
    if not snap or not snap.get("stream_path"):
        return None
    t = threading.Thread(target=_verify_thread, args=(snap,), daemon=True)
    t.start()
    if wait:
        t.join(VERIFY_TIMEOUT_S + 5)
    return t


def pipeline_snapshot():
    """O que o GET /pipeline devolve."""
    repo = REPO_DIR
    ts = turn_status()
    out = {"repo": repo, "available": False,
           "ponte": {"alive": claude_alive(), "session_id": session_id,
                     "pending": len(pending_snapshot()),
                     "turn_in_flight": ts["turn_in_flight"],
                     "last_event_age_s": ts["last_event_age_s"],
                     "verify_rodando": verify_state["running"] > 0,
                     "ultimo_verify": verify_state["last"]}}
    script = gates_script(repo)
    if not script:
        out["motivo"] = "repo sem %s" % GATES_REL
        return out
    try:
        p = subprocess.run([sys.executable, script, "pipeline"], cwd=repo, capture_output=True,
                           text=True, encoding="utf-8", errors="replace",
                           timeout=PIPELINE_TIMEOUT_S, creationflags=SUB_NO_WINDOW,
                           env=dict(os.environ, PYTHONUTF8="1", PYTHONIOENCODING="utf-8"))
        data = json.loads(p.stdout.strip().splitlines()[-1])
        out.update(available=True, status=data.get("status"), runs=data.get("runs"))
    except (OSError, subprocess.TimeoutExpired, ValueError, IndexError) as e:
        out["erro"] = "gates.py pipeline falhou: %s" % e
    return out


def turns_snapshot():
    with turn_lock:
        with turns_lock:
            return [turn_public(t) for t in turns]


# ---------------- validade do login ----------------
# `claude auth status` traz `loginExpiresAt` (CLI >= 2.1.318) e o evento
# `result` traz `credential_expires_at`. Antes de cada turno o login e
# checado: expirado (ou deslogado) bloqueia o turno; perto de expirar so avisa.
LOGIN_WARN_DAYS = 10
LOGIN_CHECK_TTL = 300  # s entre chamadas ao `claude auth status`
login_lock = threading.Lock()
login_state = {"status": "unknown", "expires_at": None, "days_left": None,
               "logged_in": None, "source": None, "checked_at": None,
               "error": None}


def parse_iso(s):
    """ISO 8601 (com 'Z') -> epoch. None se nao der para interpretar."""
    if not isinstance(s, str) or not s:
        return None
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        return None
    return dt.timestamp()


def classify_login(logged_in, expires_at, now=None):
    """-> (status, days_left). status: ok | warning | expired | logged_out | unknown"""
    if logged_in is False:
        return "logged_out", None
    exp = parse_iso(expires_at)
    if exp is None:
        return "unknown", None
    days = (exp - (now if now is not None else time.time())) / 86400.0
    if days <= 0:
        return "expired", round(days, 2)
    if days < LOGIN_WARN_DAYS:
        return "warning", round(days, 2)
    return "ok", round(days, 2)


def _set_login(logged_in, expires_at, source, error=None):
    status, days = classify_login(logged_in, expires_at)
    with login_lock:
        login_state.update({"status": status, "expires_at": expires_at,
                            "days_left": days, "logged_in": logged_in,
                            "source": source, "checked_at": time.time(),
                            "error": error})
        return dict(login_state)


def query_auth_status():
    """Roda `claude auth status --json`. -> (logged_in, loginExpiresAt, erro).
    So os campos necessarios saem daqui (a saida tem o e-mail da conta)."""
    try:
        r = subprocess.run([CLAUDE_EXE, "auth", "status", "--json"],
                           capture_output=True, text=True, timeout=30,
                           encoding="utf-8", errors="replace")
        data = json.loads(r.stdout or "{}")
    except (OSError, subprocess.SubprocessError, ValueError) as e:
        return None, None, "auth status falhou: %s" % e
    if not isinstance(data, dict):
        return None, None, "auth status: saida inesperada"
    exp = data.get("loginExpiresAt")
    err = None if exp else "auth status sem loginExpiresAt (CLI < 2.1.318?)"
    return data.get("loggedIn"), exp, err


def check_login(force=False):
    """Estado atual do login. Reconsulta o CLI se o cache venceu, se pedido,
    ou se o ultimo estado nao era ok/warning (ex.: re-login depois de expirar)."""
    with login_lock:
        st = dict(login_state)
    fresh = (st["checked_at"] is not None
             and time.time() - st["checked_at"] < LOGIN_CHECK_TTL
             and st["status"] in ("ok", "warning"))
    if fresh and not force:
        # reclassifica contra o relogio atual sem chamar o CLI de novo
        st = _set_login(st["logged_in"], st["expires_at"], st["source"], st["error"])
    else:
        logged_in, exp, err = query_auth_status()
        st = _set_login(logged_in, exp, "auth status", err)
    report_login(st)
    return st


_last_login_log = {"key": None}


def report_login(st):
    """Loga o estado do login de forma visivel (sem repetir o mesmo 'ok')."""
    s = st["status"]
    if s == "warning":
        log("!!! AVISO LOGIN: expira em %.1f dias (%s). Renove com `claude login`."
            % (st["days_left"], st["expires_at"]))
    elif s == "expired":
        log("!!! LOGIN EXPIRADO em %s - turnos bloqueados ate novo `claude login`"
            % st["expires_at"])
    elif s == "logged_out":
        log("!!! CLAUDE DESLOGADO - turnos bloqueados ate `claude login`")
    else:
        key = (s, st["expires_at"], st["error"])
        if key != _last_login_log["key"]:
            log("login: %s (expira=%s)%s" % (s, st["expires_at"],
                ("; " + st["error"]) if st["error"] else ""))
    _last_login_log["key"] = (s, st["expires_at"], st["error"])


def login_from_result(ev):
    """Atualiza a validade conhecida com o `credential_expires_at` do result."""
    exp = ev.get("credential_expires_at")
    if not exp:
        return
    with login_lock:
        same = login_state["expires_at"] == exp
        logged_in = login_state["logged_in"]
    if not same:
        report_login(_set_login(logged_in, exp, "result.credential_expires_at"))


# ---------------- modelo por turno ----------------
# Quem despacha marca o modelo de cada turno com `model` no /send. So os IDs
# abaixo (e seus apelidos) chegam ao CLI; qualquer outra string e rejeitada.
MODEL_FABLE = "claude-fable-5-2"
MODEL_OPUS = "claude-opus-5-5"
FALLBACK_MODEL = MODEL_OPUS
MODEL_ALIASES = {"fable": MODEL_FABLE, "opus": MODEL_OPUS,
                 MODEL_FABLE: MODEL_FABLE, MODEL_OPUS: MODEL_OPUS}
MODEL_PROBE_S = float(os.environ.get("BRIDGE_MODEL_PROBE_S", "3"))
MAX_MODEL_FAILURES = 2  # depois disso o modelo nao e mais tentado (fica no Opus)

current_model = None   # o --model do processo atual (None = default do CLI)
cli_model = None       # o modelo que o CLI informou no evento init
model_fallback = None  # {"requested","used","reason","ts"} da ultima queda
model_failures = {}    # modelo -> falhas de troca
turn_in_flight = False
switch_lock = threading.RLock()  # serializa todo (re)spawn do claude


def resolve_model(value):
    """-> (id_resolvido | None, erro | None). None/ausente = sem --model."""
    if value is None:
        return None, None
    if not isinstance(value, str) or value not in MODEL_ALIASES:
        return None, ("model invalido: %s (aceitos: %s)"
                      % (json.dumps(value, ensure_ascii=False)[:80],
                         ", ".join(sorted(MODEL_ALIASES))))
    return MODEL_ALIASES[value], None


def needs_model_restart(requested, current):
    """Reinicia so quando um modelo foi pedido e ele difere do corrente."""
    return requested is not None and requested != current


def is_model_error(ev):
    """O `result` de um turno que falhou por causa do modelo (ex.: 404
    'There's an issue with the selected model' no CLI 2.1.319)."""
    if not ev.get("is_error"):
        return False
    text = str(ev.get("result") or "").lower()
    return (ev.get("api_error_status") in (403, 404)
            or "selected model" in text)


def _record_fallback(requested, reason):
    global model_fallback
    model_fallback = {"requested": requested, "used": FALLBACK_MODEL,
                      "reason": reason, "ts": time.time()}
    model_failures[requested] = model_failures.get(requested, 0) + 1
    log("!!! FALLBACK DE MODELO: %s -> %s (%s; falhas=%d)"
        % (requested, FALLBACK_MODEL, reason, model_failures[requested]))


def switch_model(requested):
    """Troca o modelo da sessao preservando o contexto (--resume).
    Se o modelo pedido morrer rapido, volta para o Opus 5.5. Depois de
    MAX_MODEL_FAILURES falhas, o modelo pedido nem e mais tentado.
    -> o modelo efetivamente em uso."""
    with switch_lock:
        if not needs_model_restart(requested, current_model):
            return current_model
        if (requested != FALLBACK_MODEL
                and model_failures.get(requested, 0) >= MAX_MODEL_FAILURES):
            log("modelo %s falhou %d vezes; mantendo %s"
                % (requested, model_failures[requested], FALLBACK_MODEL))
            if current_model != FALLBACK_MODEL:
                restart_claude(model=FALLBACK_MODEL, resume=session_id)
            return FALLBACK_MODEL
        resume = session_id
        log("troca de modelo: %s -> %s (resume=%s)"
            % (current_model or "default", requested, resume))
        restart_claude(model=requested, resume=resume)
        deadline = time.time() + MODEL_PROBE_S
        while time.time() < deadline and claude_alive():
            time.sleep(0.1)
        if claude_alive():
            return requested
        if requested == FALLBACK_MODEL:
            log("!!! processo com %s morreu ao subir" % requested)
            return requested
        _record_fallback(requested, "processo morreu em < %.0fs ao subir"
                         % MODEL_PROBE_S)
        restart_claude(model=FALLBACK_MODEL, resume=resume)
        return FALLBACK_MODEL


def fallback_after_model_error(ev):
    """Turno falhou por causa do modelo: volta para o Opus 5.5 (em thread,
    para nao travar o reader). O turno que falhou nao e reenviado."""
    requested = current_model
    if requested in (None, FALLBACK_MODEL):
        return
    _record_fallback(requested, "turno falhou: %s"
                     % str(ev.get("result") or "")[:200])

    def run():
        with switch_lock:
            if current_model == requested:
                restart_claude(model=FALLBACK_MODEL, resume=ev.get("session_id")
                               or session_id)
    threading.Thread(target=run, daemon=True).start()


def model_status():
    return {"model": current_model or cli_model,
            "model_requested": current_model,
            "model_cli": cli_model,
            "model_fallback": model_fallback}


# ---------------- processo claude persistente ----------------
claude_proc = None
claude_lock = threading.Lock()
session_id = None
session_start_ts = None


def spawn_claude(model=None, resume=None):
    global claude_proc, session_id, session_start_ts, current_model, cli_model
    global turn_in_flight
    args = [CLAUDE_EXE,
            "--output-format", "stream-json",
            "--input-format", "stream-json",
            "--verbose",
            "--include-partial-messages",
            "--dangerously-skip-permissions"]
    if resume:
        args += ["--resume", resume]
    if model:
        args += ["--model", model]
    current_model = model
    cli_model = None
    reset_turn_on_spawn()
    log("iniciando claude (cwd=%s, model=%s, resume=%s)"
        % (REPO_DIR, model or "default", resume or "-"))
    err_log = open(ERR_PATH, "a", encoding="utf-8")
    with claude_lock:
        claude_proc = subprocess.Popen(
            args,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=err_log,
            cwd=REPO_DIR,
            text=True,
            bufsize=1,
            encoding="utf-8",
            errors="replace",
        )
        session_start_ts = time.time()
    session_id = resume  # com --resume a sessao continua a mesma
    threading.Thread(target=reader_loop, daemon=True).start()
    log("claude iniciado (pid=%s)" % claude_proc.pid)
    save_state()


def claude_alive():
    with claude_lock:
        return claude_proc is not None and claude_proc.poll() is None


def reader_loop():
    """Le o stdout do claude linha a linha (NDJSON) ate o processo morrer."""
    global session_id, cli_model, turn_in_flight
    proc = claude_proc
    try:
        for line in proc.stdout:
            line = line.strip()
            if not line:
                continue
            try:
                ev = json.loads(line)
            except json.JSONDecodeError:
                continue
            seq = push_event(ev)
            etype = ev.get("type")
            if proc is claude_proc:
                try:
                    on_stream_event(ev, seq)
                except Exception as e:  # vigia nunca derruba o reader
                    log("falha no batimento (seq=%s): %s" % (seq, e))
            if etype == "system" and ev.get("subtype") == "init":
                if proc is claude_proc:
                    changed = session_id != ev.get("session_id")
                    session_id = ev.get("session_id")
                    cli_model = ev.get("model") or cli_model
                    if changed:
                        log("sessao pronta: session_id=%s model=%s (seq=%s)"
                            % (session_id, cli_model, seq))
                        save_state()
            elif etype == "control_request":
                try:
                    handle_control_request(ev, seq)
                except Exception as e:  # politica nunca derruba o reader
                    log("falha na politica de control_request (seq=%s): %s"
                        % (seq, e))
            elif etype == "control_cancel_request":
                control_cancelled(ev)
            elif etype == "result":
                subtype = ev.get("subtype", "success")
                log("turno concluido: status=%s dur=%sms"
                    % ("done" if subtype == "success" else "error",
                       ev.get("duration_ms", 0)))
                try:
                    if proc is claude_proc:
                        on_result(ev, seq)
                    else:
                        record_turn(ev, seq)
                    login_from_result(ev)
                    if proc is claude_proc and is_model_error(ev):
                        fallback_after_model_error(ev)
                except Exception as e:  # registro nunca derruba o reader
                    log("falha ao registrar o turno (seq=%s): %s" % (seq, e))
    except Exception as e:
        log("reader encerrou: %s" % e)
    log("reader: stdout do claude fechou (sessao morta?)")


def write_to_claude(obj):
    """Escreve um objeto NDJSON no stdin do claude. Retorna ok/falha."""
    line = json.dumps(obj, ensure_ascii=False)
    with claude_lock:
        if claude_proc is None or claude_proc.poll() is not None:
            return False
        try:
            claude_proc.stdin.write(line + "\n")
            claude_proc.stdin.flush()
            return True
        except (BrokenPipeError, OSError) as e:
            log("stdin do claude quebrou: %s" % e)
            return False


def send_to_claude(text):
    return write_to_claude({"type": "user",
                            "message": {"role": "user", "content": text}})


def respond_to_control(request_id, response):
    """Responde um control_request (ex.: aprovacao do MCP)."""
    ok = write_to_claude({"type": "control_response",
                          "response": {"subtype": "success",
                                       "request_id": request_id,
                                       "response": response}})
    if ok:
        with pending_lock:
            pending.pop(request_id, None)
            pending_deadline.pop(request_id, None)
        capture_turn_event({"type": "control_response", "request_id": request_id,
                            "origem": "ponte", "ts": time.time()})
        log("control_response enviado: id=%s" % request_id)
    return ok


_interrupt_n = [0]


def interrupt_turn(reason):
    """Pede ao CLI para interromper o turno atual (control_request interrupt;
    o `init` anuncia a capacidade e o CLI 2.1.322 responde control_response
    success). Nunca reenvia o brief."""
    _interrupt_n[0] += 1
    rid = "bridge-interrupt-%d-%d" % (os.getpid(), _interrupt_n[0])
    ok = write_to_claude({"type": "control_request", "request_id": rid,
                          "request": {"subtype": "interrupt"}})
    bridge_alert("interrupt", "action", "interrupt enviado (%s): %s"
                 % ("ok" if ok else "FALHOU", reason),
                 request_id=rid, ok=ok, reason=reason)
    return ok


def restart_claude(model=None, resume=None):
    """Mata o claude e sobe outro. Sem `model`, mantem o modelo corrente."""
    with switch_lock:  # o vigia nao pode ressuscitar no meio
        _restart_claude(model, resume)


def _restart_claude(model, resume):
    if model is None:
        model = current_model
    log("reiniciando sessao do claude...")
    with claude_lock:
        p = claude_proc
    if p is not None and p.poll() is None:
        try:
            p.terminate()
            p.wait(timeout=10)
        except Exception:
            try:
                p.kill()
            except Exception:
                pass
    with pending_lock:
        pending.clear()
        pending_deadline.clear()
    spawn_claude(model=model, resume=resume)


# ---------------- estado persistente (F10) ----------------
# repo, modelo e sessao sobrevivem ao reinicio da ponte: antes, a tarefa
# voltava no repo do argumento (10:18:39 de 11/11, nootrieapp) e numa sessao nova.
_saved_state = {"v": None}


def load_state(path=None):
    """-> dict (vazio se nao ha arquivo ou se ele e invalido)."""
    try:
        with open(path or STATE_PATH, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def save_state():
    """Grava repo/modelo/sessao de forma atomica, so quando mudou."""
    data = {"repo": REPO_DIR, "model": current_model, "session_id": session_id}
    if data == _saved_state["v"]:
        return
    out = dict(data, updated_at=time.time())
    tmp = STATE_PATH + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(out, f, ensure_ascii=False, indent=1)
        os.replace(tmp, STATE_PATH)
        _saved_state["v"] = data
    except OSError as e:
        log("state.json falhou: %s" % e)


def restore_plan(state, argv_repo):
    """-> (repo, model, resume, motivo). Pura. A sessao so e retomada no
    mesmo repo em que ela nasceu (o CLI guarda sessoes por pasta)."""
    repo = state.get("repo")
    if not (isinstance(repo, str) and repo and os.path.isdir(repo)):
        return argv_repo, None, None, ("sem state.json" if not state
                                       else "repo do state.json invalido")
    model = state.get("model")
    model = model if model in (MODEL_FABLE, MODEL_OPUS) else None
    sid = state.get("session_id")
    sid = sid if isinstance(sid, str) and sid else None
    return repo, model, sid, "restaurado do state.json"


def start_claude(model=None, resume=None):
    """Sobe o claude; se o --resume nao pegar (processo morre logo), sobe sem."""
    with switch_lock:
        spawn_claude(model=model, resume=resume)
        if not resume:
            return
        deadline = time.time() + MODEL_PROBE_S
        while time.time() < deadline and claude_alive():
            time.sleep(0.1)
        if claude_alive():
            return
        bridge_alert("resume_falhou", "warn",
                     "--resume %s nao subiu; sessao nova no mesmo repo" % resume,
                     session_id=resume)
        spawn_claude(model=model, resume=None)


# ---------------- vigia (um thread, um tique por WATCH_TICK_S) ----------------
WATCH_TICK_S = float(os.environ.get("BRIDGE_WATCH_TICK_S", "1"))
AUTO_RESPAWN = os.environ.get("BRIDGE_AUTO_RESPAWN", "1") == "1"
RESPAWN_MAX, RESPAWN_WINDOW_S = 3, 600.0
_respawns = []
_respawn_capped = [False]


def maybe_respawn(now=None):
    """O claude morreu sozinho (C6): retoma a sessao com --resume. No maximo
    RESPAWN_MAX vezes em RESPAWN_WINDOW_S, para nao virar laco."""
    if not AUTO_RESPAWN:
        return
    now = time.time() if now is None else now
    with claude_lock:
        p = claude_proc
    if p is None or p.poll() is None:
        return
    if not switch_lock.acquire(blocking=False):
        return  # troca de modelo / restart em andamento cuida disso
    try:
        with claude_lock:
            if claude_proc is not p:
                return
        _respawns[:] = [t for t in _respawns if now - t < RESPAWN_WINDOW_S]
        if len(_respawns) >= RESPAWN_MAX:
            if not _respawn_capped[0]:
                _respawn_capped[0] = True
                bridge_alert("claude_morto", "action",
                             "claude morreu %d vezes em %.0f min; nao retomo mais "
                             "sozinho (POST /restart)" % (RESPAWN_MAX, RESPAWN_WINDOW_S / 60))
            return
        _respawn_capped[0] = False
        _respawns.append(now)
        bridge_alert("claude_morto", "action",
                     "processo do claude morreu (exit=%s); retomando com --resume %s"
                     % (p.poll(), session_id or "-"),
                     exit_code=p.poll(), session_id=session_id)
        start_claude(model=current_model, resume=session_id)
    finally:
        switch_lock.release()


def watchdog_tick(now=None):
    now = time.time() if now is None else now
    expire_pending(now)
    watch_turn(now)
    maybe_respawn(now)


def watchdog_loop():
    while True:
        time.sleep(WATCH_TICK_S)
        try:
            watchdog_tick()
        except Exception as e:  # o vigia nunca morre
            log("vigia: erro no tique: %s" % e)


# ---------------- screenshot (debug visual) ----------------
SCREENSHOT_PS = r"""
Add-Type -AssemblyName System.Windows.Forms, System.Drawing
$bounds = [System.Windows.Forms.Screen]::PrimaryScreen.Bounds
$bmp = New-Object System.Drawing.Bitmap($bounds.Width, $bounds.Height)
$g = [System.Drawing.Graphics]::FromImage($bmp)
$g.CopyFromScreen($bounds.Location, [System.Drawing.Point]::Empty, $bounds.Size)
$bmp.Save($args[0], [System.Drawing.Imaging.ImageFormat]::Png)
$g.Dispose(); $bmp.Dispose()
"""


def take_screenshot():
    """Captura a tela principal e salva em SHOT_PATH. Retorna ok/falha."""
    try:
        r = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command",
             SCREENSHOT_PS, SHOT_PATH],
            capture_output=True, text=True, timeout=30)
        if r.returncode != 0:
            log("screenshot falhou: %s" % (r.stderr or "")[:200])
            return False
        return os.path.isfile(SHOT_PATH)
    except Exception as e:
        log("screenshot falhou: %s" % e)
        return False


def tail(path, n):
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            return f.readlines()[-n:]
    except OSError:
        return []

TRANSCRIPT_HTML = r"""<!DOCTYPE html>
<html lang="pt-BR">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Ponte Muse &#8596; Claude Code &mdash; ao vivo</title>
<style>
*{box-sizing:border-box}
body{margin:0;font-family:system-ui,-apple-system,'Segoe UI',Roboto,sans-serif;background:#0f1115;color:#e8eaf0}
header{position:sticky;top:0;background:#171a21;border-bottom:1px solid #2a2f3a;padding:10px 14px;display:flex;align-items:center;gap:10px;z-index:10}
header h1{font-size:15px;margin:0;font-weight:600}
#st{font-size:12px;color:#9aa3b2;font-family:monospace}
#pauseBtn{margin-left:auto;background:#232833;color:#e8eaf0;border:1px solid #39414f;border-radius:8px;padding:6px 12px;font-size:13px;cursor:pointer}
#pending{display:none;max-width:860px;margin:12px auto 0;background:#2a2110;border:1px solid #8a6d1f;border-radius:12px;padding:10px 12px;font-size:13px;line-height:1.5}
#pending b{color:#f0c44c}
#pending pre{white-space:pre-wrap;overflow-wrap:break-word;font-size:12px;color:#d8c58a;margin:6px 0 0}
#log{max-width:860px;margin:0 auto;padding:14px 12px 60px}
.msg{border-radius:12px;padding:10px 12px;margin:10px 0;background:#171a21;border:1px solid #2a2f3a}
.msg .meta{display:flex;justify-content:space-between;margin-bottom:6px;font-size:12px}
.who{font-weight:700}
.ts{color:#8b93a5;font-family:monospace}
.msg.muse{border-left:3px solid #7c6cf0}
.msg.muse .who{color:#b3a7ff}
.msg.claude{border-left:3px solid #4caf7d}
.msg.claude .who{color:#7fd6a4}
.msg.result-ok{border-left:3px solid #4caf7d;background:#14231b}
.msg.result-err{border-left:3px solid #e05c5c;background:#2a1616}
.msg.sys{background:transparent;border:1px dashed #39414f;text-align:center;font-size:13px;color:#9aa3b2}
.msg .body{font-size:14px;line-height:1.55;overflow-wrap:break-word}
.msg .body p{margin:0 0 8px}
.msg .body pre{background:#0b0d11;border-radius:8px;padding:8px;overflow-x:auto;font-size:12px}
.msg .body code{font-family:monospace;font-size:13px}
details.tool{margin:6px 0;font-size:13px;color:#9aa3b2}
details.tool summary{cursor:pointer}
.rmeta{font-size:12px;color:#8b93a5;margin-bottom:6px}
.deny{background:#2a2110;border:1px solid #8a6d1f;border-radius:8px;padding:6px 8px;margin:0 0 8px;font-size:13px}
.deny b{color:#f0c44c}
.deny pre{white-space:pre-wrap;font-size:12px;color:#d8c58a;margin:4px 0 0}
.empty{color:#8b93a5;text-align:center;margin-top:40px;font-size:14px}
</style>
</head>
<body>
<header>
<h1>&#128268; Ponte Muse &#8596; Claude Code</h1>
<span id="st">conectando&hellip;</span>
<button id="pauseBtn">&#10074;&#10074; pausar</button>
</header>
<div id="pending"></div>
<main id="log"><div class="empty" id="empty">carregando hist&oacute;rico&hellip;</div></main>
<script>
var since=0, paused=false, busy=false;
function esc(s){return String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');}
function fmt(s){return esc(s).replace(/\n/g,'<br>');}
function hhmmss(ts){try{return new Date(ts*1000).toLocaleTimeString('pt-BR',{hour:'2-digit',minute:'2-digit',second:'2-digit'});}catch(e){return '';}}
function addMsg(cls,who,html,ts){
  if(paused)return;
  var empty=document.getElementById('empty'); if(empty)empty.remove();
  var log=document.getElementById('log');
  var div=document.createElement('div');
  div.className='msg '+cls;
  div.innerHTML='<div class="meta"><span class="who">'+who+'</span><span class="ts">'+(ts?hhmmss(ts):'')+'</span></div><div class="body">'+html+'</div>';
  log.appendChild(div);
  window.scrollTo(0,document.body.scrollHeight);
}
function render(ev){
  var t=ev.type;
  if(t==='user'){
    var c=(ev.message||{}).content||'';
    addMsg('muse','Muse',fmt(typeof c==='string'?c:JSON.stringify(c)),ev.ts);
  }else if(t==='assistant'){
    var blocks=((ev.message||{}).content)||[], html='';
    for(var i=0;i<blocks.length;i++){var b=blocks[i];
      if(b.type==='text'&&b.text)html+='<p>'+fmt(b.text)+'</p>';
      else if(b.type==='tool_use')html+='<details class="tool"><summary>&#128295; '+esc(b.name||'tool')+'</summary><pre>'+fmt(JSON.stringify(b.input||{},null,1).slice(0,800))+'</pre></details>';
    }
    if(html)addMsg('claude','Claude Code',html,ev.ts);
  }else if(t==='result'){
    var ok=(ev.subtype||'success')==='success';
    var meta='dura&ccedil;&atilde;o '+Math.round((ev.duration_ms||0)/1000)+'s'+(ev.total_cost_usd?' &middot; $'+Number(ev.total_cost_usd).toFixed(2):'');
    var pd=ev.permission_denials, den='';
    if(!Array.isArray(pd))den='<div class="rmeta">permiss&otilde;es negadas: n&atilde;o reportado</div>';
    else if(pd.length)den='<div class="deny"><b>&#9888; '+pd.length+' permiss&atilde;o(&otilde;es) negada(s)</b><pre>'+fmt(JSON.stringify(pd,null,1).slice(0,1200))+'</pre></div>';
    addMsg(ok?'result-ok':'result-err','Resultado '+(ok?'&#10003;':'&#10007;'),
      '<div class="rmeta">'+meta+'</div>'+den+'<p>'+fmt(ev.result||'(sem texto)')+'</p>',ev.ts);
  }else if(t==='system'&&(ev.subtype==='init'||ev.subtype==='restart')){
    addMsg('sys','Sistema',ev.subtype==='init'?'Sess&atilde;o iniciada &middot; <code>'+esc(ev.session_id||'')+'</code>':'Sess&atilde;o reiniciada',ev.ts);
  }else if(t==='control_request'){
    addMsg('sys','Sistema','&#9203; Aguardando decis&atilde;o: <code>'+esc(ev.request_id||'')+'</code>',ev.ts);
  }else if(t==='bridge_alert'){
    addMsg(ev.level==='info'?'sys':'result-err','Ponte &middot; '+esc(ev.kind||'alerta'),fmt(ev.message||''),ev.ts);
  }
}
async function poll(){
  if(busy)return; busy=true;
  try{
    var r=await fetch('/events?since='+since+'&timeout=25');
    var d=await r.json();
    var evs=d.events||[];
    for(var i=0;i<evs.length;i++){var ev=evs[i]; if(ev.seq>since)since=ev.seq; render(ev);}
  }catch(e){}
  busy=false;
  setTimeout(poll,400);
}
async function status(){
  try{
    var r=await fetch('/status'); var s=await r.json();
    document.getElementById('st').textContent=(s.alive?'\u25CF sess\u00E3o viva':'\u25CB sess\u00E3o morta')+(s.session_id?' \u00B7 '+s.session_id.slice(0,8)+'\u2026':'')+(s.pending?' \u00B7 '+s.pending+' pendente(s)':'')+(function(l){l=l||{};if(l.status==='warning')return ' \u00B7 \u26A0 login expira em '+l.days_left+' dias';if(l.status==='expired'||l.status==='logged_out')return ' \u00B7 \u26D4 login '+l.status;return '';})(s.login);
  }catch(e){document.getElementById('st').textContent='sem conex\u00E3o';}
  setTimeout(status,10000);
}
async function pending(){
  try{
    var r=await fetch('/pending'); var d=await r.json();
    var box=document.getElementById('pending');
    if(d.pending&&d.pending.length){
      var p=d.pending[0];
      box.style.display='block';
      box.innerHTML='<b>&#9203; Aguardando decis&atilde;o da Muse</b> ('+d.pending.length+' pendente(s)) &middot; id <code>'+esc(p.request_id||'')+'</code><pre>'+fmt(JSON.stringify(p,null,1).slice(0,600))+'</pre>';
    }else{box.style.display='none';}
  }catch(e){}
  setTimeout(pending,5000);
}
document.getElementById('pauseBtn').onclick=function(){
  paused=!paused; this.innerHTML=paused?'&#9654; retomar':'&#10074;&#10074; pausar';
};
status(); poll(); pending();
</script>
</body>
</html>
"""


# ---------------- /file: baixar arquivos do repo atual ----------------
# Fecha o loop visual: a Muse baixa os renders que o turno gravou no repo.
# So le, so dentro da raiz do repo atual (REPO_DIR, o que o /switch-repo define),
# so extensoes de imagem/texto, com teto de tamanho. Mesmo modelo de confianca
# do resto da ponte: tailnet-only, sem token.
FILE_TYPES = {
    ".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
    ".webp": "image/webp", ".gif": "image/gif",
    ".txt": "text/plain; charset=utf-8", ".md": "text/markdown; charset=utf-8",
    ".json": "application/json; charset=utf-8", ".log": "text/plain; charset=utf-8",
}
FILE_MAX_BYTES = 20 * 1024 * 1024


def resolve_repo_file(repo_dir, rel):
    """-> (http_code, caminho_absoluto | mensagem_de_erro, content_type | None).

    Pura (sem HTTP) para ser testavel. Rejeita caminho absoluto, drive, UNC,
    ':' (streams NTFS) e NUL; resolve links/junctions com realpath e exige que
    o alvo fique dentro da raiz resolvida do repo.
    """
    if not rel or not isinstance(rel, str):
        return 400, "parametro path ausente", None
    if "\x00" in rel:
        return 400, "path invalido", None
    if os.path.isabs(rel) or rel.startswith(("/", "\\")) or os.path.splitdrive(rel)[0][:1] in ("\\", "/") \
            or (len(rel) >= 3 and rel[1] == ":" and rel[2] in "\\/"):
        return 403, "path deve ser relativo ao repo", None
    if ":" in rel:  # "C:rel" (relativo a drive) e streams NTFS "a.md:x"
        return 400, "path invalido", None
    root = os.path.realpath(repo_dir)
    full = os.path.realpath(os.path.join(root, rel))
    nroot, nfull = os.path.normcase(root), os.path.normcase(full)
    try:
        inside = os.path.commonpath([nroot, nfull]) == nroot
    except ValueError:  # drives diferentes
        inside = False
    if not inside or nfull == nroot:
        return 403, "fora da raiz do repo", None
    ctype = FILE_TYPES.get(os.path.splitext(full)[1].lower())
    if ctype is None:
        return 415, "extensao nao servida", None
    if not os.path.isfile(full):
        return 404, "arquivo inexistente", None
    if os.path.getsize(full) > FILE_MAX_BYTES:
        return 413, "arquivo maior que %d MB" % (FILE_MAX_BYTES // (1024 * 1024)), None
    return 200, full, ctype


class BridgeHTTPServer(ThreadingHTTPServer):
    """No Windows, SO_REUSEADDR deixa um segundo processo escutar na MESMA
    porta; aqui a porta ocupada tem que falhar o bind (F21)."""
    allow_reuse_address = os.name != "nt"
    daemon_threads = True

    def server_bind(self):
        if os.name == "nt" and hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        super().server_bind()


BIND_RETRY_S = float(os.environ.get("BRIDGE_BIND_RETRY_S", "90"))


def bind_http():
    """Faz o bind ANTES de abrir o claude. No boot o IP do Tailscale pode ainda
    nao existir: tenta por BIND_RETRY_S. -> servidor ou None."""
    deadline = time.time() + BIND_RETRY_S
    tries = 0
    while True:
        tries += 1
        try:
            return BridgeHTTPServer((BIND_IP, PORT), Handler)
        except OSError as e:
            if time.time() >= deadline:
                log("!!! bind em %s:%d falhou %d vez(es) (%s); saindo SEM abrir o claude"
                    % (BIND_IP, PORT, tries, e))
                return None
            if tries == 1 or tries % 10 == 0:
                log("bind em %s:%d falhou (%s); tentando de novo por ate %.0fs"
                    % (BIND_IP, PORT, e, BIND_RETRY_S))
            time.sleep(min(2.0, max(0.2, BIND_RETRY_S / 5)))


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _file(self, rel):
        code, target, ctype = resolve_repo_file(REPO_DIR, rel)
        if code != 200:
            return self._json({"ok": False, "error": target, "path": rel}, code)
        try:
            size = os.path.getsize(target)
            f = open(target, "rb")
        except OSError:
            return self._json({"ok": False, "error": "falha ao ler", "path": rel}, 500)
        with f:
            self.send_response(200)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(size))
            self.send_header("Content-Disposition",
                             'inline; filename="%s"' % os.path.basename(target).replace('"', ""))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            while True:
                chunk = f.read(256 * 1024)
                if not chunk:
                    break
                self.wfile.write(chunk)
        log("/file servido: %s (%d bytes)" % (rel, size))

    def _json(self, obj, code=200):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _html(self, html, code=200):
        body = html.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _png(self, path):
        try:
            with open(path, "rb") as f:
                body = f.read()
        except OSError:
            return self._json({"ok": False, "error": "screenshot ausente"}, 500)
        self.send_response(200)
        self.send_header("Content-Type", "image/png")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        u = urlparse(self.path)
        q = parse_qs(u.query)

        if u.path == "/health":
            return self._json({"ok": True, "ts": time.time(),
                               "session": "alive" if claude_alive() else "dead",
                               "session_id": session_id})

        if u.path == "/status":
            with events_lock:
                n = len(events)
                last = next_seq - 1
            up = round(time.time() - session_start_ts, 1) if session_start_ts else 0
            return self._json({"alive": claude_alive(), "session_id": session_id,
                               "events": n, "last_seq": last, "uptime_s": up,
                               "pending": len(pending_snapshot()),
                               "repo": REPO_DIR,
                               "login": dict(login_state),
                               "last_turn": (turns_snapshot() or [None])[-1],
                               **turn_status(),
                               "log_files": log_files_status(),
                               **model_status()})

        if u.path == "/turns":
            return self._json({"turns": turns_snapshot()})

        if u.path == "/pipeline":
            return self._json(pipeline_snapshot())

        if u.path == "/events":
            try:
                since = int(q.get("since", ["0"])[0])
            except ValueError:
                since = 0
            try:
                timeout = min(float(q.get("timeout", ["25"])[0]), 60)
            except ValueError:
                timeout = 25
            deadline = time.time() + timeout
            while True:
                out, info = events_since(since)
                if out or time.time() >= deadline:
                    break
                time.sleep(0.4)
            if info["gap"]:  # o poller perdeu eventos: dizer, nao esconder (F12)
                return self._json({"events": out, **info})
            return self._json({"events": out})

        if u.path == "/transcript":
            return self._html(TRANSCRIPT_HTML)

        if u.path == "/pending":
            return self._json({"pending": pending_snapshot()})

        if u.path == "/screenshot":
            if take_screenshot():
                return self._png(SHOT_PATH)
            return self._json({"ok": False,
                               "error": "falha ao capturar a tela"}, 500)

        if u.path == "/file":
            return self._file(q.get("path", [""])[0])

        if u.path == "/logs":
            try:
                n = min(int(q.get("lines", ["100"])[0]), 500)
            except ValueError:
                n = 100
            return self._json({"server": tail(LOG_PATH, n),
                               "stderr": tail(ERR_PATH, n)})

        return self._json({"error": "not found"}, 404)

    def do_POST(self):
        global turn_in_flight
        u = urlparse(self.path)
        try:
            n = int(self.headers.get("Content-Length", 0))
        except ValueError:
            n = 0
        try:
            data = json.loads(self.rfile.read(n).decode("utf-8") or "{}")
        except (json.JSONDecodeError, UnicodeDecodeError):
            data = {}

        if u.path == "/send":
            text = data.get("text", "")
            if not text or not isinstance(text, str):
                return self._json({"ok": False, "error": "text vazio"}, 400)
            requested, err = resolve_model(data.get("model"))
            if err:
                return self._json({"ok": False, "error": err}, 400)
            queue = data.get("queue") is True
            meta, err = parse_send_meta(data)
            if err:
                return self._json({"ok": False, "error": err}, 400)
            switching = needs_model_restart(requested, current_model)
            if turn_in_flight and claude_alive() and not switching and not queue:
                return self._json({"ok": False, "error":
                                   "turno em andamento (use \"queue\": true para "
                                   "enfileirar de proposito, ou POST /interrupt)",
                                   **turn_status()}, 409)
            if not switching and not claude_alive():
                return self._json({"ok": False, "error": "sessao morta"}, 500)
            login = check_login()
            if login["status"] in ("expired", "logged_out"):
                log("turno NAO iniciado: login %s" % login["status"])
                return self._json({"ok": False,
                                   "error": "login %s" % ("expirado" if login["status"] == "expired" else "ausente"),
                                   "login": login}, 403)
            if switching:
                if turn_in_flight and claude_alive():
                    return self._json({"ok": False, "error":
                                       "turno em andamento; troca de modelo so "
                                       "entre turnos"}, 409)
                switch_model(requested)
                if not claude_alive():
                    return self._json({"ok": False, "error": "sessao morta",
                                       **model_status()}, 500)
            with events_lock:
                after_seq = next_seq - 1
            if not send_to_claude(text):
                return self._json({"ok": False,
                                   "error": "falha ao escrever no claude"}, 500)
            queued = turn_in_flight and queue
            user_seq = push_event({"type": "user",
                                   "message": {"role": "user", "content": text}})
            b = open_turn(user_seq, current_model, queued=queued, meta=meta, text=text)
            if not queued and not (meta.get("budget_min") and meta.get("budget_usd")):
                log("turno %s sem budget_min/budget_usd: o gate B3 vai falhar no verify" % b["brief_id"])
            log("turno %s (%d chars; after_seq=%d; model=%s; brief=%s)"
                % ("enfileirado" if queued else "iniciado", len(text), after_seq,
                   current_model or "default", b["brief_id"]))
            out = {"ok": True, "after_seq": after_seq, "brief_id": b["brief_id"],
                   "queued": queued, **model_status(),
                   "budget": {"budget_min": b.get("budget_min"), "budget_usd": b.get("budget_usd")}}
            if login["status"] == "warning":
                out["login_warning"] = ("login expira em %.1f dias (%s)"
                                        % (login["days_left"], login["expires_at"]))
            return self._json(out)

        if u.path == "/respond":
            rid = data.get("request_id", "")
            resp = data.get("response", {})
            if not rid or not isinstance(resp, dict):
                return self._json({"ok": False,
                                   "error": "request_id/response invalidos"}, 400)
            if not claude_alive():
                return self._json({"ok": False, "error": "sessao morta"}, 500)
            if respond_to_control(rid, resp):
                return self._json({"ok": True})
            return self._json({"ok": False,
                               "error": "falha ao escrever no claude"}, 500)

        if u.path == "/interrupt":
            if not claude_alive():
                return self._json({"ok": False, "error": "sessao morta"}, 500)
            if not turn_in_flight and data.get("force") is not True:
                return self._json({"ok": False, "error": "sem turno em voo "
                                   "(\"force\": true manda mesmo assim)"}, 409)
            reason = str(data.get("reason") or "pedido via POST /interrupt")[:200]
            mark_interrupt("interrompido", reason)
            ok = interrupt_turn(reason)
            return self._json({"ok": ok, **turn_status()}, 200 if ok else 500)

        if u.path == "/restart":
            restart_claude()
            return self._json({"ok": True,
                               "session": "alive" if claude_alive() else "dead"})

        if u.path == "/switch-repo":
            global REPO_DIR
            path = data.get("path", "")
            if not path or not isinstance(path, str) or not os.path.isdir(path):
                return self._json({"ok": False,
                                   "error": "diretorio invalido"}, 400)
            REPO_DIR = os.path.abspath(path)
            log("repo trocado para %s" % REPO_DIR)
            restart_claude()  # sessao nova no repo novo; o spawn grava o state.json
            return self._json({"ok": True, "repo": REPO_DIR,
                               "session": "alive" if claude_alive() else "dead"})

        return self._json({"error": "not found"}, 404)


def main():
    global REPO_DIR
    log("== ponte v2 - sessao persistente via stream-json (pid=%d) ==" % os.getpid())
    repo, model, resume, why = restore_plan(load_state(), REPO_DIR)
    if repo != REPO_DIR:
        log("repo do argumento (%s) substituido pelo state.json: %s" % (REPO_DIR, repo))
    REPO_DIR = repo
    log("repo=%s bind=%s:%d model=%s resume=%s (%s)"
        % (REPO_DIR, BIND_IP, PORT, model or "default", resume or "-", why))
    if not os.path.isfile(CLAUDE_EXE):
        log("AVISO: claude nao encontrado em %s (env CLAUDE_EXE p/ override)"
            % CLAUDE_EXE)
    srv = bind_http()  # F21: sem porta, nao abre claude nenhum
    if srv is None:
        sys.exit(2)
    n = replay_jsonl()
    if n:
        log("historico restaurado do disco: %d eventos" % n)
    check_login(force=True)
    start_claude(model=model, resume=resume)
    threading.Thread(target=watchdog_loop, daemon=True).start()
    log("HTTP no ar em http://%s:%d" % (BIND_IP, PORT))
    log("Transcricao ao vivo em http://%s:%d/transcript" % (BIND_IP, PORT))
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        log("encerrando (Ctrl+C)")
    finally:
        with claude_lock:
            p = claude_proc
        if p is not None and p.poll() is None:
            p.terminate()


if __name__ == "__main__":
    main()
