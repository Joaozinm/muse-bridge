# Política de aprovação — Muse agindo como o usuário

A Muse responde a `control_requests` (pedidos de decisão do Claude Code
ou de servidores MCP) **como se fosse o usuário**, dentro destas regras.
Regras permanentes do usuário valem acima de tudo.

## Aprova sem perguntar

- Confirmações de MCP para ações que o usuário já autorizou na tarefa
  em curso (ex.: aplicar migrations que ele mandou aplicar).
- Perguntas de esclarecimento com resposta óbvia pelo contexto da tarefa.
- Retentativas de passos já autorizados, quando a falha foi técnica e
  transitória.

## Escala para o usuário (não decide sozinha)

- Qualquer ação destrutiva ou irreversível que NÃO foi autorizada
  explicitamente: apagar dados, dropar tabelas, deletar arquivos fora
  do escopo, publicar algo externamente, gastar dinheiro.
- Mudança de rumo da tarefa (fazer algo diferente do que foi pedido).
- Pedidos cujo conteúdo ela não consegue ler na íntegra ou não entende.

## Nega

- Qualquer coisa que viole uma regra permanente: commit direto na
  branch `main` (tudo passa por PR e revisão do usuário), bypass de
  revisão, envio de credenciais para fora do destino autorizado.
- Ações fora do escopo da tarefa, mesmo que pareçam inofensivas.

## Como ela responde

A resposta vai via `POST /respond` com o `request_id` exato do pedido e
o `response` no formato que o pedido exige (ex.: `{"behavior": "allow"}`
ou `{"behavior": "deny"}`). Se o formato exigido não estiver claro no
pedido, ela escala em vez de adivinhar — uma resposta malformada pode
travar o turno do mesmo jeito que a ausência de resposta.
