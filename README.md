# MuseBridge v2

Ponte mínima entre a Muse e o Claude Code rodando no PC do usuário.

## Instalar (uma vez)

```powershell
git clone https://github.com/Joaozinm/muse-bridge.git C:\muse-bridge
cd C:\muse-bridge
powershell -ExecutionPolicy Bypass -File Install.ps1
```

O instalador cria a tarefa agendada `MuseBridge`: inicia no logon,
reinicia sozinha se cair, não abre janela de terminal, não duplica
instância. Depois de instalar, a ponte está no ar — abra a transcrição:

```
http://<IP-Tailscale-do-PC>:8000/transcript
```

## Como funciona

- `server.py` (só biblioteca padrão do Python) mantém **um** processo
  `claude` vivo em modo stream-json.
- A Muse manda tarefas com `POST /send`; o servidor escreve no stdin do
  Claude e guarda os eventos do stdout num buffer + num arquivo
  `events.jsonl` (o histórico sobrevive a restarts).
- A Muse lê os resultados com `GET /events` (long-poll).
- O usuário assiste tudo ao vivo em `GET /transcript`.

## A Muse como o usuário: aprovações

Quando o Claude Code — ou um servidor MCP, como o do Supabase — precisa
de uma decisão humana, ele emite um `control_request` no stream. O
servidor guarda o pedido **inteiro** em `GET /pending` e o mostra na
página de transcrição. A Muse lê o pedido, decide e responde com
`POST /respond` — o turno continua. Sem resposta, o pedido fica esperando
para sempre (foi exatamente o que travou as escritas no Supabase em
outubro/2026: a confirmação do MCP nunca era respondida numa sessão
headless).

Política da Muse ao aprovar em nome do usuário (ver `POLICY.md`):
aprova o que já foi autorizado na tarefa; escala para o usuário o que é
destrutivo/irreversível e não foi autorizado; nega o que viola regras
permanentes (ex.: commit direto na `main`).

## Modelo por turno

Quem despacha escolhe o modelo de cada turno com o campo `model` do
`/send`: planejamento em **Claude Fable 5.2**, execução em **Claude Opus 5.5**.

| `model` | vira |
|---|---|
| `fable` ou `claude-fable-5-2` | `--model claude-fable-5-2` |
| `opus` ou `claude-opus-5-5` | `--model claude-opus-5-5` |
| ausente / `null` | nada muda (sem `model`, o processo segue como está; no boot, default do CLI) |
| qualquer outra coisa | `400 model invalido` — a string nunca chega ao CLI |

- **Troca só quando muda.** Se o modelo pedido é o da sessão atual, o turno
  vai direto. Se é outro, o servidor reinicia o `claude` com
  `--resume <session_id> --model <id>`: mesma sessão, mesmo contexto. Sem
  `session_id` ainda, sobe só com `--model`.
- **Só entre turnos.** Pedir outro modelo com um turno em andamento devolve
  `409` (reiniciar ali mataria o turno e as aprovações pendentes).
- **Fallback.** Se o processo com o modelo pedido morre nos primeiros
  segundos, ou o turno falha por causa do modelo (o CLI devolve `result`
  com `is_error` e `api_error_status` 404/"selected model"), a sessão volta
  para `claude-opus-5-5` (com `--resume`) e o motivo fica em
  `/status.model_fallback`. O turno que falhou **não** é reenviado — quem
  despacha vê o erro e decide. Depois de 2 falhas do mesmo modelo, ele nem é
  mais tentado: os turnos seguem no Opus 5.5 até a ponte reiniciar.
- `/status` traz `model` (o modelo em uso), `model_requested` (o `--model`
  do processo; `null` = default do CLI), `model_cli` (o que o CLI informou no
  `init`) e `model_fallback`.

## Auto-diagnóstico

Quando a Muse detecta um travamento (sem eventos novos por minutos, ou
só `tool_progress` sem progresso), ela pode, sozinha:
- ler `GET /logs` (log do servidor + stderr do Claude, na íntegra);
- ver `GET /screenshot` (a tela do PC, para enxergar diálogos e erros);
- ver `GET /status` (processo vivo? sessão? pendências?);
- reiniciar a sessão com `POST /restart` e tentar de novo.

Screenshots são usados só para depurar o trabalho do agente.

## Endpoints

| Rota | Uso |
|---|---|
| `GET /health` | servidor vivo? sessão viva? |
| `GET /status` | sessão, eventos, pendências, uptime, repo atual, `model`, `model_fallback` |
| `POST /send` | `{"text": "...", "model": "fable"}` → manda tarefa (`model` opcional, ver abaixo) |
| `GET /events` | `?since=N&timeout=S` → long-poll de eventos |
| `GET /transcript` | página ao vivo |
| `GET /pending` | control_requests sem resposta |
| `POST /respond` | `{"request_id": "...", "response": {...}}` |
| `GET /screenshot` | PNG da tela principal |
| `GET /logs` | `?lines=N` → logs |
| `POST /restart` | reinicia a sessão do Claude |
| `POST /switch-repo` | `{"path": "..."}` → troca o repo (reinicia a sessão com o novo cwd). A esteira usa isso para atender vários projetos com uma só ponte, um de cada vez. |

## Segurança (decisão deliberada)

O servidor escuta **somente** no IP Tailscale. A rede privada é a
fronteira de acesso — não há token nesta versão. Um token criaria um
problema de bootstrap/segredo maior que o risco que ele fecha numa
tailnet de um único usuário.

## Atualizar / desinstalar

Atualizar: `git pull` na pasta e reiniciar a tarefa (ou só aguardar o
próximo logon). Reiniciar a tarefa — o cmdlet `Restart-ScheduledTask`
NÃO existe neste PowerShell; use Stop + Start:

```powershell
Stop-ScheduledTask -TaskName MuseBridge
Start-ScheduledTask -TaskName MuseBridge
```

Se o restart for feito pelo próprio Claude Code, ele precisa agendar
destacado com atraso (senão o `Stop` mata o turno antes do `Start`):

```powershell
Start-Process powershell -ArgumentList '-NoProfile','-Command','Start-Sleep -Seconds 20; Stop-ScheduledTask -TaskName MuseBridge; Start-ScheduledTask -TaskName MuseBridge' -WindowStyle Hidden
```

Desinstalar:

```powershell
powershell -ExecutionPolicy Bypass -File Install.ps1 -Uninstall
```

## O que a v2 removeu em relação à v1

Banco `queue.db`, endpoints `/msg` e `/poll`, distribuição via branch
`muse/bridge` dentro do repo do Nootrie, launcher manual com terminal
aberto. A v1 exigia babysitting; a v2 é um utilitário invisível.
