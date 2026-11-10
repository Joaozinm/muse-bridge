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
                                        "model":...,"model_fallback":...}
  POST /send        {"text": "...", "model": "fable"|"opus"|<id>}  (model opcional)
                                     -> {"ok":true,"after_seq":N,"model":...}
  GET  /events?since=N&timeout=S     -> {"events":[...]} (long-poll)
  GET  /transcript                   -> pagina com a transcricao ao vivo
  GET  /pending                      -> {"pending":[...]} (control_requests sem resposta)
  POST /respond {"request_id": "...", "response": {...}} -> {"ok":true}
  GET  /screenshot                   -> PNG da tela principal
  GET  /logs?lines=N                 -> {"server": [...], "stderr": [...]}
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

os.makedirs(BRIDGE_DIR, exist_ok=True)

# Console do Windows nem sempre e UTF-8; nao deixar isso derrubar o servidor.
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

MAX_EVENTS = 5000


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


def push_event(ev):
    """Guarda o evento na memoria e no JSONL (historico sobrevive a restart)."""
    global next_seq
    with events_lock:
        ev["seq"] = next_seq
        ev["ts"] = time.time()
        next_seq += 1
        events.append(ev)
        if len(events) > MAX_EVENTS:
            del events[:len(events) - MAX_EVENTS]
    try:
        with open(JSONL_PATH, "a", encoding="utf-8") as f:
            f.write(json.dumps(ev, ensure_ascii=False) + "\n")
    except OSError as e:
        log("JSONL falhou: %s" % e)
    return ev["seq"]


def replay_jsonl():
    """Recarrega o historico do disco na memoria (apos restart do servidor)."""
    global next_seq
    try:
        with open(JSONL_PATH, encoding="utf-8") as f:
            lines = f.readlines()[-MAX_EVENTS:]
    except OSError:
        return 0
    n = 0
    with events_lock:
        for line in lines:
            line = line.strip()
            if not line:
                continue
            try:
                ev = json.loads(line)
            except json.JSONDecodeError:
                continue
            events.append(ev)
            next_seq = max(next_seq, ev.get("seq", 0) + 1)
            n += 1
    return n


# ---------------- control_requests pendentes ----------------
pending = {}
pending_lock = threading.Lock()


def pending_snapshot():
    with pending_lock:
        return [dict(v) for v in pending.values()]


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
    """Registra o turno concluido e suas negacoes (log + /status + /turns).
    Nunca levanta excecao: falhar aqui nao pode derrubar o reader."""
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
    with turns_lock:
        turns.append(turn)
        del turns[:-MAX_TURNS]
    return turn


def turns_snapshot():
    with turns_lock:
        return [dict(t) for t in turns]


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
switch_lock = threading.Lock()


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
    turn_in_flight = False
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
            if etype == "system" and ev.get("subtype") == "init":
                session_id = ev.get("session_id")
                cli_model = ev.get("model") or cli_model
                log("sessao pronta: session_id=%s model=%s (seq=%s)"
                    % (session_id, cli_model, seq))
            elif etype == "control_request":
                rid = ev.get("request_id", "?")
                with pending_lock:
                    pending[rid] = ev
                log("control_request pendente: id=%s (seq=%s) — ver /pending"
                    % (rid, seq))
            elif etype == "result":
                subtype = ev.get("subtype", "success")
                log("turno concluido: status=%s dur=%sms"
                    % ("done" if subtype == "success" else "error",
                       ev.get("duration_ms", 0)))
                if proc is claude_proc:
                    turn_in_flight = False
                try:
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
        log("control_response enviado: id=%s" % request_id)
    return ok


def restart_claude(model=None, resume=None):
    """Mata o claude e sobe outro. Sem `model`, mantem o modelo corrente."""
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
    spawn_claude(model=model, resume=resume)


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


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

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
                               **model_status()})

        if u.path == "/turns":
            return self._json({"turns": turns_snapshot()})

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
            out = []
            while True:
                with events_lock:
                    out = [e for e in events if e["seq"] > since]
                if out or time.time() >= deadline:
                    break
                time.sleep(0.4)
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
            switching = needs_model_restart(requested, current_model)
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
            turn_in_flight = True
            push_event({"type": "user",
                        "message": {"role": "user", "content": text}})
            log("turno iniciado (%d chars; after_seq=%d; model=%s)"
                % (len(text), after_seq, current_model or "default"))
            out = {"ok": True, "after_seq": after_seq, **model_status()}
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
            restart_claude()
            return self._json({"ok": True, "repo": REPO_DIR,
                               "session": "alive" if claude_alive() else "dead"})

        return self._json({"error": "not found"}, 404)


def main():
    log("== ponte v2 - sessao persistente via stream-json ==")
    log("repo=%s bind=%s:%d" % (REPO_DIR, BIND_IP, PORT))
    if not os.path.isfile(CLAUDE_EXE):
        log("AVISO: claude nao encontrado em %s (env CLAUDE_EXE p/ override)"
            % CLAUDE_EXE)
    n = replay_jsonl()
    if n:
        log("historico restaurado do disco: %d eventos" % n)
    check_login(force=True)
    spawn_claude()
    srv = ThreadingHTTPServer((BIND_IP, PORT), Handler)
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
