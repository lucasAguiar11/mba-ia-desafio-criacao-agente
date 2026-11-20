# Residencial Aurora: assistente virtual

Assistente do aplicativo dos moradores, feito com **Google ADK 2.15.0** e exposto por uma API **FastAPI** em `http://localhost:8000`. Pelo chat o morador reserva e cancela áreas comuns, autoriza visitantes e tira dúvidas sobre o regulamento.

O modelo conduz a conversa, mas as regras críticas ficam no código: o apartamento vem da sessão, cobranças e acessos dependem da rota de confirmações, e a exclusividade da reserva é garantida pelo banco.

```
aurora/
  agent.py     agentes e tools (ADK)
  api.py       API HTTP (FastAPI + Runner + sessões em SQLite)
  db.py        dados do condomínio em SQLite; `python -m aurora.db` restaura o estado inicial
  checagem.py  checagem das regras de gravação, sem modelo
dados/         estado inicial do condomínio (não é alterado)
estado/        criado em tempo de execução: aurora.db (reservas/visitantes) e sessoes.db (sessões ADK)
```

## Arquitetura

```
            POST /sessoes/{id}/mensagens  |  POST /sessoes/{id}/confirmacoes
                                   │
                         Runner (ADK) + SqliteSessionService
                                   │
                    assistente_aurora  (agente principal)
          transfer_to_agent ┌──────┴───────┐ transfer_to_agent      AgentTool
                            ▼              ▼                          ▼
                        reservas       visitantes                regulamento
             listar_minhas_reservas    listar_meus_visitantes    ler_capitulo
             consultar_disponibilidade autorizar_visitante (confirmação)
             reservar_area (confirmação se taxa > 0)
             cancelar_reserva
                            │              │
                            └──── aurora/db.py (SQLite) ────┘
```

| Agente | Responsabilidade | Como é acionado | Por quê |
| --- | --- | --- | --- |
| `assistente_aurora` (principal) | Recebe o morador e distribui o trabalho. Não tem tools de dados nem o regulamento nas instruções. | É o `root_agent` do `App`, chamado pelo `Runner`. | Ponto único de entrada; só roteia. |
| `reservas` | Lista, consulta disponibilidade, reserva e cancela reservas do apartamento da sessão. | Sub-agente em modo *chat*, por `transfer_to_agent`. | As tools de reserva pedem confirmação (`request_confirmation`). Como sub-agente, a execução roda **na sessão persistida**, então o pedido de confirmação e a retomada ficam gravados nela, e o Runner devolve a resposta ao agente que pediu (`find_agent_to_run` → autor da chamada). Depois da transferência ele segue atendendo o morador sem uma chamada extra ao principal. |
| `visitantes` | Lista e autoriza visitantes do apartamento da sessão. | Sub-agente em modo *chat*, por `transfer_to_agent`. | Mesmo motivo: `autorizar_visitante` sempre pede confirmação, e ela precisa sobreviver na sessão persistida. |
| `regulamento` | Responde dúvidas lendo **um** capítulo de `dados/regulamento.md`. | `AgentTool` do agente principal. | O `AgentTool` roda o especialista num runner aninhado com sessão própria em memória. O capítulo lido fica nessa sessão descartável, e na sessão do morador entra só a resposta final. O texto do regulamento não acompanha as mensagens seguintes (Garantia 4). |

Todos os agentes usam `gemini-3.1-flash-lite` por padrão (configurável por `AURORA_MODEL`). É um modelo estável, rápido e suficiente para escolher tools simples. Foi o que rodou o fluxo inteiro do avaliador dentro da cota do plano gratuito; no teste, `gemini-2.5-flash` estourou o limite de 20 requisições/dia do free tier. As chamadas ao Gemini repetem com backoff em 429/5xx (`retry_options` em `aurora/agent.py`), então um pico de demanda não vira erro 500 na API.

**Armazenamento:** SQLite (biblioteca padrão, sem serviço externo).
- `estado/aurora.db`: tabelas `reservas` e `visitantes`. Cancelar marca `ativa = 0` e mantém a linha, para o código nunca ser reaproveitado.
- `estado/sessoes.db`: sessões e eventos do ADK (`SqliteSessionService`).

## Garantias

### Garantia 1: cobrança ou acesso só com confirmação

- **`aurora/agent.py`, `reservar_area` (linhas 65–94):** se a área tem `taxa > 0` e a chamada ainda não traz `tool_context.tool_confirmation`, a tool chama `tool_context.request_confirmation(hint, payload={"area", "data", "taxa"})` e retorna **sem gravar**. Ela só grava quando o ADK a reexecuta com `tool_confirmation.confirmed == True`. Se for negada, retorna `nao_reservado`. Área com taxa 0 (quadra) grava direto, sem confirmação.
- **`aurora/agent.py`, `autorizar_visitante` (linhas 116–141):** sempre pede confirmação, com `payload={"nome", "data"}`, e só chama `db.autorizar_visitante` depois de aprovada.
- **`aurora/api.py`, `_pendentes` (linhas 59–75):** as pendências são as chamadas `adk_request_confirmation` gravadas na sessão que ainda não têm resposta. `acao` é o nome da tool original e `detalhes` é o `payload` validado pela tool.
- **`aurora/api.py`, `responder_confirmacao` (linhas 104–110):** se o `id` não está em `_pendentes(sessao)`, a rota responde **409** e não chama o Runner. Isso cobre id inexistente, id de outra sessão e id já respondido, porque a resposta gravada tira o id da lista. Se o id for válido, a rota envia um `FunctionResponse(name="adk_request_confirmation", response={"confirmed": ...})` ao Runner, que retoma a tool no agente que pediu.

**Por que não depende do modelo:** a rota de mensagens só aceita texto (`types.Part(text=...)`). Escrever "já confirmei" não produz um `FunctionResponse`. O `tool_confirmation` só existe quando a rota de confirmações envia a resposta, e o ADK ainda confere se ela corresponde à chamada original gravada na sessão (`_resolve_confirmation_targets`).

### Garantia 2: cada sessão pertence a um apartamento

- **`aurora/api.py`, `criar_sessao` (linhas 87–95):** o apartamento é validado contra `dados/apartamentos.json` e gravado **uma única vez** no state da sessão (`state={"apartamento": ...}`). Nenhuma rota ou tool altera esse valor depois.
- **`aurora/agent.py`, `_apartamento` (linha 25):** todas as tools que leem ou gravam dados do morador obtêm o apartamento de `tool_context.state["apartamento"]`. **Nenhuma tool recebe apartamento como parâmetro**, então o modelo não tem como escolher outro.
- **`aurora/db.py`, `cancelar_reserva` (linhas 117–124):** o `UPDATE` filtra por `codigo = ? AND apartamento = ?`. Um código de outro apartamento não cancela nada, e a resposta não diz se ele existe.
- **`aurora/db.py`, `data_livre` (linhas 87–92) e `consultar_disponibilidade` em `agent.py`:** devolvem só `livre: true/false`, nunca o código ou o apartamento da reserva existente. `reservar_area` também responde apenas "já está reservado(a)".

**Por que não depende do modelo:** mesmo que o morador diga ser do 302, as tools só enxergam o apartamento da sessão. Os dados de outro apartamento nunca são retornados a uma tool, então não chegam aos eventos nem à resposta.

### Garantia 3: nada se perde no reinício

- **`aurora/api.py`, linhas 26–27:** `SqliteSessionService("estado/sessoes.db")` guarda sessões e eventos em disco, e o `Runner` usa esse serviço.
- **`aurora/db.py`:** reservas e visitantes ficam em `estado/aurora.db`. `iniciar()` (linhas 43–49) só carrega os dados iniciais num banco novo, então reiniciar a API não sobrescreve nada.

**Por que não depende do modelo:** a persistência fica nos serviços de sessão e de dados. Uma confirmação pedida antes do reinício pode ser aprovada depois dele, porque o pedido e a chamada original estão gravados na sessão.

### Garantia 4: o regulamento é consultado, não carregado

- **`aurora/agent.py`, `CAPITULOS` e `ler_capitulo` (linhas 146–163):** o regulamento é dividido por capítulo, e a tool devolve **um** capítulo por vez.
- **`aurora/agent.py`, `regulamento_agent` (linhas 207–218):** só o especialista recebe o **índice** dos capítulos (títulos) nas instruções. O texto completo não está em nenhuma instrução.
- **`aurora/agent.py`, `root_agent` (linhas 220–234):** as instruções do principal não contêm o regulamento. Ele aciona o especialista por `AgentTool(regulamento_agent)`.

**Por que não depende do modelo:** o `AgentTool` executa o especialista num runner aninhado com `InMemorySessionService` próprio. A chamada a `ler_capitulo` e o texto do capítulo ficam nessa sessão descartável, e na sessão do morador entram só a pergunta e a resposta final (por exemplo, "até as 20h, conforme o Art. 22, II"). Mesmo que o especialista leia o capítulo errado, esse texto não vai para os eventos.

### Garantia 5: dois moradores, uma reserva

- **`aurora/db.py`, `SCHEMA` (linhas 25–26):**
  ```sql
  CREATE UNIQUE INDEX uma_reserva_ativa_por_area_data ON reservas (area, data) WHERE ativa = 1;
  ```
  O SQLite confere a exclusividade **no próprio INSERT**: um segundo INSERT ativo para a mesma área e data falha com `IntegrityError`, mesmo que as duas gravações tenham passado pela conferência prévia ao mesmo tempo.
- **`aurora/db.py`, `criar_reserva` (linhas 95–114):** captura o `IntegrityError` do índice e devolve `None`, e `reservar_area` transforma isso numa resposta normal ("já está reservado(a)"). As duas aprovações respondem 200. O código `RSV-XXXXXXXX` é a `PRIMARY KEY` (linha 18) e reservas canceladas não são apagadas, então um código nunca se repete.
- **`aurora/checagem.py`:** dispara 20 gravações simultâneas na mesma área e data, **sem** a conferência prévia, e verifica que só uma vence (`uv run python -m aurora.checagem`).

**Por que não depende do modelo:** a conferência `data_livre` antes da gravação existe só para responder rápido. Quem garante a regra é o índice único do banco, no instante da gravação.

## Como rodar

**Pré-requisitos:** [uv](https://docs.astral.sh/uv/). O uv instala o Python 3.13 se precisar. Também é preciso uma chave do [Google AI Studio](https://aistudio.google.com/apikey). Não há serviço externo para subir: o SQLite é local.

**Variáveis do `.env`** (copie de `.env.example`):

| Variável | Uso |
| --- | --- |
| `GOOGLE_API_KEY` | Chave do Google AI Studio (obrigatória). |
| `GOOGLE_GENAI_USE_VERTEXAI` | `FALSE` (usa o AI Studio, não a Vertex). |
| `AURORA_MODEL` | Opcional. Modelo Gemini dos agentes; vazio = `gemini-3.1-flash-lite`. |

```bash
cp .env.example .env          # preencha GOOGLE_API_KEY
uv sync

# restaurar os dados iniciais (reservas e visitantes voltam ao estado de dados/)
uv run python -m aurora.db

# subir a API em http://localhost:8000
uv run uvicorn aurora.api:app --port 8000
```

O comando de restauração recria só reservas e visitantes a partir de `dados/*.json`; as sessões (`estado/sessoes.db`) são mantidas. Para apagar também as sessões, pare a API e remova a pasta `estado/`; a próxima subida recria tudo com os dados iniciais.

Checagem das regras de gravação, sem chamar o modelo: `uv run python -m aurora.checagem`.

Para ver transferências e confirmações no `adk web` durante o desenvolvimento, o agente está em `aurora/agent.py` (`root_agent`).

### Contrato da API

| Rota | Resposta |
| --- | --- |
| `POST /sessoes` `{"apartamento": "101"}` | `201 {"session_id": "..."}` |
| `POST /sessoes/{id}/mensagens` `{"texto": "..."}` | `200 {"resposta": "...", "confirmacoes_pendentes": [{"id", "acao", "detalhes"}]}` |
| `POST /sessoes/{id}/confirmacoes` `{"id": "...", "confirmado": true}` | `200`, mesmo formato; `409` se o id não está pendente nesta sessão |
| `GET /sessoes/{id}/eventos` | `200`, lista completa de eventos em ordem |
| `GET /apartamentos/{n}/reservas` | `200 [{"codigo", "area", "data"}]` (só reservas ativas) |
| `GET /apartamentos/{n}/visitantes` | `200 [{"nome", "data"}]` |

As rotas com `{id}` respondem `404` quando a sessão não existe.
