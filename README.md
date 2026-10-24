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
| `GET /status` | sessão, eventos, pendências, uptime |
| `POST /send` | `{"text": "..."}` → manda tarefa |
| `GET /events` | `?since=N&timeout=S` → long-poll de eventos |
| `GET /transcript` | página ao vivo |
| `GET /pending` | control_requests sem resposta |
| `POST /respond` | `{"request_id": "...", "response": {...}}` |
| `GET /screenshot` | PNG da tela principal |
| `GET /logs` | `?lines=N` → logs |
| `POST /restart` | reinicia a sessão do Claude |

## Segurança (decisão deliberada)

O servidor escuta **somente** no IP Tailscale. A rede privada é a
fronteira de acesso — não há token nesta versão. Um token criaria um
problema de bootstrap/segredo maior que o risco que ele fecha numa
tailnet de um único usuário.

## Atualizar / desinstalar

Atualizar: `git pull` na pasta e reiniciar a tarefa (ou só aguardar o
próximo logon). Desinstalar:

```powershell
powershell -ExecutionPolicy Bypass -File Install.ps1 -Uninstall
```

## O que a v2 removeu em relação à v1

Banco `queue.db`, endpoints `/msg` e `/poll`, distribuição via branch
`muse/bridge` dentro do repo do Nootrie, launcher manual com terminal
aberto. A v1 exigia babysitting; a v2 é um utilitário invisível.
