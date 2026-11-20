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
          transfer_to_agent ┌──────┴───────┐ transfer_to_agent   tool (mode=single_turn)
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
| `regulamento` | Responde dúvidas lendo **um** capítulo de `dados/regulamento.md`. | Sub-agente com `mode="single_turn"`: o ADK o expõe ao principal como a tool `regulamento(request)` e o executa inline, num sub-branch da sessão. | Uma dúvida de regulamento é uma pergunta e uma resposta, sem diálogo, então `single_turn` basta e devolve o controle ao principal. É a forma que o código do ADK 2.15 recomenda no lugar de `AgentTool` (a docstring de `AgentTool` diz que o uso direto é desencorajado). O capítulo lido fica só em state `temp:`, então não vai para os eventos nem acompanha as mensagens seguintes (Garantia 4). |

Todos os agentes usam `gemini-3.1-flash-lite` por padrão (configurável por `AURORA_MODEL`). É um modelo estável, rápido e suficiente para escolher tools simples. Foi o que rodou o fluxo inteiro do avaliador dentro da cota do plano gratuito; no teste, `gemini-2.5-flash` estourou o limite de 20 requisições/dia do free tier. As chamadas ao Gemini repetem com backoff em 429/5xx (`retry_options` em `aurora/agent.py`), então um pico de demanda não vira erro 500 na API.

Os nomes seguem a API atual da série 2, conferida no código instalado: `from google.adk import Agent, Context` (`ToolContext` e `LlmAgent` são aliases), confirmação dinâmica com `Context.request_confirmation`, `Runner(app=App(...))` e `SqliteSessionService`, que é o serviço que o próprio ADK usa para URIs `sqlite://` e no armazenamento local do `adk web`.

**Armazenamento:** SQLite (biblioteca padrão, sem serviço externo).
- `estado/aurora.db`: tabelas `reservas` e `visitantes`. Cancelar marca `ativa = 0` e mantém a linha, para o código nunca ser reaproveitado.
- `estado/sessoes.db`: sessões e eventos do ADK (`SqliteSessionService`).

## Garantias

### Garantia 1: cobrança ou acesso só com confirmação

- **`aurora/agent.py`, `reservar_area` (linhas 67–96):** se a área tem `taxa > 0` e a chamada ainda não traz `tool_context.tool_confirmation`, a tool chama `tool_context.request_confirmation(hint, payload={"area", "data", "taxa"})` e retorna **sem gravar**. Ela só grava quando o ADK a reexecuta com `tool_confirmation.confirmed == True`. Se for negada, retorna `nao_reservado`. Área com taxa 0 (quadra) grava direto, sem confirmação.
- **`aurora/agent.py`, `autorizar_visitante` (linhas 118–143):** sempre pede confirmação, com `payload={"nome", "data"}`, e só chama `db.autorizar_visitante` depois de aprovada.
- **`aurora/api.py`, `_pendentes` (linhas 59–75):** as pendências são as chamadas `adk_request_confirmation` gravadas na sessão que ainda não têm resposta. `acao` é o nome da tool original e `detalhes` é o `payload` validado pela tool.
- **`aurora/api.py`, `responder_confirmacao` (linhas 108–114):** se o `id` não está em `_pendentes(sessao)`, a rota responde **409** e não chama o Runner. Isso cobre id inexistente, id de outra sessão e id já respondido, porque a resposta gravada tira o id da lista. Se o id for válido, a rota envia um `FunctionResponse(name="adk_request_confirmation", response={"confirmed": ...})` ao Runner, que retoma a tool no agente que pediu.

**Por que não depende do modelo:** a rota de mensagens só aceita texto (`types.Part(text=...)`). Escrever "já confirmei" não produz um `FunctionResponse`. O `tool_confirmation` só existe quando a rota de confirmações envia a resposta, e o ADK ainda confere se ela corresponde à chamada original gravada na sessão (`_resolve_confirmation_targets`).

### Garantia 2: cada sessão pertence a um apartamento

- **`aurora/api.py`, `criar_sessao` (linhas 91–99):** o apartamento é validado contra `dados/apartamentos.json` e gravado **uma única vez** no state da sessão (`state={"apartamento": ...}`). Nenhuma rota ou tool altera esse valor depois.
- **`aurora/agent.py`, `_apartamento` (linha 25):** todas as tools que leem ou gravam dados do morador obtêm o apartamento de `tool_context.state["apartamento"]`. **Nenhuma tool recebe apartamento como parâmetro**, então o modelo não tem como escolher outro.
- **`aurora/db.py`, `cancelar_reserva` (linhas 115–122):** o `UPDATE` filtra por `codigo = ? AND apartamento = ?`. Um código de outro apartamento não cancela nada, e a resposta não diz se ele existe.
- **`aurora/db.py`, `data_livre` (linhas 85–90) e `consultar_disponibilidade` em `agent.py`:** devolvem só `livre: true/false`, nunca o código ou o apartamento da reserva existente. `reservar_area` também responde apenas "já está reservado(a)".

**Por que não depende do modelo:** mesmo que o morador diga ser do 302, as tools só enxergam o apartamento da sessão. Os dados de outro apartamento nunca são retornados a uma tool, então não chegam aos eventos nem à resposta.

### Garantia 3: nada se perde no reinício

- **`aurora/api.py`, linhas 26–27:** `SqliteSessionService("estado/sessoes.db")` guarda sessões e eventos em disco, e o `Runner` usa esse serviço.
- **`aurora/db.py`:** reservas e visitantes ficam em `estado/aurora.db`. `iniciar()` (linhas 43–49) só carrega os dados iniciais num banco novo, então reiniciar a API não sobrescreve nada.

**Por que não depende do modelo:** a persistência fica nos serviços de sessão e de dados. Uma confirmação pedida antes do reinício pode ser aprovada depois dele, porque o pedido e a chamada original estão gravados na sessão.

### Garantia 4: o regulamento é consultado, não carregado

- **`aurora/agent.py`, `CAPITULOS` e `ler_capitulo` (linhas 149–173):** o regulamento é dividido por capítulo. A tool grava **um** capítulo em `tool_context.state["temp:capitulo_regulamento"]` e devolve só `{"status": "Capítulo IV carregado..."}`.
- **`aurora/agent.py`, `_instrucao_regulamento` e `regulamento_agent` (linhas 176–236):** o especialista recebe nas instruções o **índice** dos capítulos e, depois de `ler_capitulo`, o capítulo carregado (lido do state `temp:` por um `InstructionProvider`).
- **`aurora/agent.py`, `root_agent` (linhas 238–251):** as instruções do principal não contêm o regulamento. Ele só vê a tool `regulamento(request)` e recebe a resposta final.

**Por que não depende do modelo:** state com prefixo `temp:` é aplicado só à sessão em memória durante a invocação e é removido do `state_delta` do evento antes de gravar (`BaseSessionService._apply_temp_state` e `_trim_temp_delta_state` em `google/adk/sessions/base_session_service.py`). Por isso o texto carregado do regulamento nunca é gravado em evento. Entram só `ler_capitulo({"capitulo": "IV"})`, o status curto e a resposta final do especialista, que traz apenas o que responde à pergunta (por exemplo, "das 9h às 20h, conforme o Art. 22, II"). Mesmo que o especialista carregue o capítulo errado, o texto desse capítulo não chega aos eventos. O único texto livre gravado é a resposta, e a instrução do especialista a restringe ao que foi perguntado.

### Garantia 5: dois moradores, uma reserva

- **`aurora/db.py`, `SCHEMA` (linhas 25–26):**
  ```sql
  CREATE UNIQUE INDEX uma_reserva_ativa_por_area_data ON reservas (area, data) WHERE ativa = 1;
  ```
  O SQLite confere a exclusividade **no próprio INSERT**: um segundo INSERT ativo para a mesma área e data falha com `IntegrityError`, mesmo que as duas gravações tenham passado pela conferência prévia ao mesmo tempo.
- **`aurora/agent.py`, `_validar` (linhas 29–43), e `aurora/db.py`, `CHECK` da coluna `data` (linha 21):** antes de consultar ou gravar, a data é reescrita na forma canônica `AAAA-MM-DD`: `2030-W11-6` e `20300316` viram `2030-03-16`. O banco recusa qualquer outro formato. Como o índice compara texto, isso garante que o mesmo dia é sempre escrito do mesmo jeito e não pode ser reservado duas vezes.
- **`aurora/db.py`, `criar_reserva` (linhas 93–112):** captura o `IntegrityError` do índice e devolve `None`, e `reservar_area` transforma isso numa resposta normal ("já está reservado(a)"). As duas aprovações respondem 200. O código `RSV-XXXXXXXX` é a `PRIMARY KEY` (linha 18) e reservas canceladas não são apagadas, então um código nunca se repete.
- **`aurora/checagem.py`:** dispara 20 gravações simultâneas na mesma área e data, **sem** a conferência prévia, e verifica que só uma vence (`uv run python -m aurora.checagem`).

**Por que não depende do modelo:** a conferência `data_livre` antes da gravação existe só para responder rápido. Quem garante a regra é o índice único do banco, no instante da gravação.

## Como rodar

**Pré-requisitos:** [uv](https://docs.astral.sh/uv/). O uv instala o Python 3.13 se precisar. Também é preciso uma chave do [Google AI Studio](https://aistudio.google.com/apikey). Não há serviço externo para subir: o SQLite é local.

**Variáveis do `.env`** (copie de `.env.example`):

| Variável | Uso |
| --- | --- |
| `GOOGLE_API_KEY` | Chave do Google AI Studio (obrigatória). |
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
