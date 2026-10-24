#!/usr/bin/env python3
"""Ponte Muse <-> Claude Code v2 (server.py) — versao enxuta.

Um unico processo `claude` fica vivo o tempo todo; a Muse manda mensagens
via HTTP e recebe os eventos do stream em tempo real. Sem banco de fila,
sem endpoints legados: o stream de eventos e a unica fonte de verdade.

Uso:  python server.py [REPO_DIR] [BIND_IP]
Env:  CLAUDE_EXE  (padrao: ~/.local/bin/claude.exe)
      BRIDGE_DIR  (padrao: ~/.muse-bridge)
      PYTHONUTF8=1

Endpoints:
  GET  /health                       -> {"ok":true,"session":"alive"|"dead",...}
  GET  /status                       -> {"alive":...,"session_id":...,"pending":N,...}
  POST /send        {"text": "..."}  -> {"ok":true,"after_seq":N}
  GET  /events?since=N&timeout=S     -> {"events":[...]} (long-poll)
  GET  /transcript                   -> pagina com a transcricao ao vivo
  GET  /pending                      -> {"pending":[...]} (control_requests sem resposta)
  POST /respond {"request_id": "...", "response": {...}} -> {"ok":true}
  GET  /screenshot                   -> PNG da tela principal
  GET  /logs?lines=N                 -> {"server": [...], "stderr": [...]}
  POST /restart                      -> mata o claude e comeca sessao nova

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
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

PORT = 8000
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


# ---------------- processo claude persistente ----------------
claude_proc = None
claude_lock = threading.Lock()
session_id = None
session_start_ts = None


def spawn_claude():
    global claude_proc, session_id, session_start_ts
    args = [CLAUDE_EXE,
            "--output-format", "stream-json",
            "--input-format", "stream-json",
            "--verbose",
            "--include-partial-messages",
            "--dangerously-skip-permissions"]
    log("iniciando claude (cwd=%s)" % REPO_DIR)
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
    session_id = None
    threading.Thread(target=reader_loop, daemon=True).start()
    log("claude iniciado (pid=%s)" % claude_proc.pid)


def claude_alive():
    with claude_lock:
        return claude_proc is not None and claude_proc.poll() is None


def reader_loop():
    """Le o stdout do claude linha a linha (NDJSON) ate o processo morrer."""
    global session_id
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
                log("sessao pronta: session_id=%s (seq=%s)" % (session_id, seq))
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
                          "request_id": request_id,
                          "response": response})
    if ok:
        with pending_lock:
            pending.pop(request_id, None)
        log("control_response enviado: id=%s" % request_id)
    return ok


def restart_claude():
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
    spawn_claude()


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
    addMsg(ok?'result-ok':'result-err','Resultado '+(ok?'&#10003;':'&#10007;'),
      '<div class="rmeta">'+meta+'</div><p>'+fmt(ev.result||'(sem texto)')+'</p>',ev.ts);
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
    document.getElementById('st').textContent=(s.alive?'\u25CF sess\u00E3o viva':'\u25CB sess\u00E3o morta')+(s.session_id?' \u00B7 '+s.session_id.slice(0,8)+'\u2026':'')+(s.pending?' \u00B7 '+s.pending+' pendente(s)':'');
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
                               "pending": len(pending_snapshot())})

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
            if not claude_alive():
                return self._json({"ok": False, "error": "sessao morta"}, 500)
            with events_lock:
                after_seq = next_seq - 1
            if not send_to_claude(text):
                return self._json({"ok": False,
                                   "error": "falha ao escrever no claude"}, 500)
            push_event({"type": "user",
                        "message": {"role": "user", "content": text}})
            log("turno iniciado (%d chars; after_seq=%d)" % (len(text), after_seq))
            return self._json({"ok": True, "after_seq": after_seq})

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
