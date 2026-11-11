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

**Prazo e política automática (F01 da auditoria de 11/11).** Para não
travar de novo, a ponte responde sozinha o que a tabela `ELICITATION_POLICY`
conhece (hoje: recusa o opt-in de telemetria do `blender-community`). Para
qualquer outro pedido, a Muse tem 60 s (`BRIDGE_ELICIT_WAIT_S`); sem
resposta, a ponte cancela o pedido e encerra o turno com `interrupt`
(decisão D3, opção B). Tudo isso aparece como evento `bridge_alert`.
Detalhes em `POLICY.md`.

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

## Vigia de turno (F02, F06, F13, F23 da auditoria)

Um **turno** é um brief: tudo o que um `/send` dispara, inclusive os
`result` extras de quando o CLI volta a trabalhar sozinho (uma tarefa em
background terminou). Prazos (decisão D4 do João):

| Situação | Aviso | Ação |
|---|---|---|
| Nenhum evento com turno em voo | 120 s (`bridge_alert` `silencio`, com nº de pendências e screenshot) | 300 s: `interrupt`, turno `stalled` |
| Chamada MCP (`mcp__*`) em voo | 30 s (`mcp_lenta`) | 120 s: `interrupt`, turno `mcp_longa` |
| Duração do turno | — | 45 min: `interrupt`, turno `teto` |
| Interrupt sem `result` em 15 s | — | turno fechado com `cost_turn_usd: null`, `cost_reason: "sem result"` |
| Processo do `claude` morreu | — | turno `morto` |

- `/send` com turno em voo devolve **409**. Para enfileirar de propósito:
  `"queue": true` (a mensagem entra no mesmo brief).
- `POST /interrupt` `{"reason": "..."}` interrompe o turno em voo (409 se não
  houver, a menos que `"force": true`). A ponte nunca reenvia o brief.
- `result` com tarefa em background aberta não fecha o turno: ele fica
  `esperando_background` até a tarefa terminar e vir o próximo `result`.
- Custo: o `total_cost_usd` do CLI é acumulado **por processo** (zera
  quando o processo sobe de novo, mesmo com `--resume`). A ponte publica
  `cost_turn_usd` (delta, somado por brief) e `cost_session_usd` (o valor
  cru) em `/turns` e `/status.last_turn`.
- `/status` traz `turn_in_flight`, `turn` (brief, idade, estado, results),
  `last_event_age_s`, `tool_in_flight`, `background_tasks` e `limits`.
- Os prazos podem ser mudados por env (`BRIDGE_SILENCE_WARN_S`,
  `BRIDGE_SILENCE_KILL_S`, `BRIDGE_TURN_MAX_S`, `BRIDGE_MCP_WARN_S`,
  `BRIDGE_MCP_KILL_S`), mas o valor de produção é o da tabela.

## Estado persistente e partida (F10, F21)

- `~/.muse-bridge/state.json` guarda `repo`, `model` e `session_id`, gravado
  (de forma atômica) a cada mudança. Na partida, se o repo do arquivo existe,
  ele **vence o argumento da tarefa** e a sessão é retomada com
  `--resume <session_id> --model <model>`. Se o `--resume` não sobe, a
  ponte abre sessão nova no mesmo repo (`bridge_alert` `resume_falhou`).
- O bind HTTP acontece **antes** de abrir o `claude`. Se a porta ou o IP não
  estão disponíveis (ex.: Tailscale ainda subindo no boot), a ponte tenta por
  90 s (`BRIDGE_BIND_RETRY_S`) e sai com código 2 **sem abrir claude nenhum**.
  No Windows o bind é exclusivo (`SO_EXCLUSIVEADDRUSE`): uma segunda ponte não
  consegue escutar na mesma porta.
- Se o `claude` morre sozinho, o vigia fecha o turno em voo como `morto` e
  retoma a sessão com `--resume` (no máximo 3 vezes em 10 min; depois só com
  `POST /restart`).
- A tarefa agendada (Install.ps1) roda o python via `cmd.exe` e manda o
  stderr para `~/.muse-bridge/task-stderr.log`. Vale a partir da próxima
  execução do `Install.ps1`.

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
| `GET /status` | sessão, eventos, pendências, uptime, repo atual, `model`, `model_fallback`, `turn_in_flight`, `turn`, `last_event_age_s`, `tool_in_flight` |
| `POST /send` | `{"text": "...", "model": "fable", "queue": false}` → manda tarefa (`model` e `queue` opcionais); 409 com turno em voo |
| `POST /interrupt` | `{"reason": "..."}` → interrompe o turno em voo |
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
