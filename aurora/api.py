"""API HTTP do assistente. Subir: `uv run uvicorn aurora.api:app --port 8000`."""

import json
from contextlib import asynccontextmanager

from dotenv import load_dotenv

load_dotenv()  # antes de importar o agente: GOOGLE_API_KEY e AURORA_MODEL vêm do .env

from fastapi import FastAPI, HTTPException  # noqa: E402
from google.adk.apps import App  # noqa: E402
from google.adk.runners import Runner  # noqa: E402
from google.adk.sessions.sqlite_session_service import SqliteSessionService  # noqa: E402
from google.genai import types  # noqa: E402
from pydantic import BaseModel  # noqa: E402

from . import db  # noqa: E402
from .agent import root_agent  # noqa: E402

APP_NAME = "aurora"
USER_ID = "morador"  # ponytail: um user fixo; quem é o morador é o apartamento gravado no state da sessão
CONFIRMACAO = "adk_request_confirmation"
APARTAMENTOS = {a["numero"] for a in json.loads((db.DADOS / "apartamentos.json").read_text())}

db.ESTADO.mkdir(exist_ok=True)
sessoes = SqliteSessionService(str(db.ESTADO / "sessoes.db"))  # Garantia 3: sessões em disco
runner = Runner(app=App(name=APP_NAME, root_agent=root_agent), session_service=sessoes)


@asynccontextmanager
async def lifespan(_: FastAPI):
    db.iniciar()
    yield


app = FastAPI(title="Assistente Residencial Aurora", lifespan=lifespan)


class NovaSessao(BaseModel):
    apartamento: str


class Mensagem(BaseModel):
    texto: str


class Resposta(BaseModel):
    id: str
    confirmado: bool


async def _sessao(session_id: str):
    sessao = await sessoes.get_session(app_name=APP_NAME, user_id=USER_ID, session_id=session_id)
    if sessao is None:
        raise HTTPException(404, "Sessão não encontrada.")
    return sessao


def _pendentes(sessao) -> list[dict]:
    """Pedidos de confirmação do ADK (adk_request_confirmation) ainda sem resposta na sessão."""
    respondidas = {r.id for e in sessao.events for r in e.get_function_responses() if r.name == CONFIRMACAO}
    pendentes = []
    for evento in sessao.events:
        for chamada in evento.get_function_calls():
            if chamada.name == CONFIRMACAO and chamada.id not in respondidas:
                original = chamada.args["originalFunctionCall"]
                pedido = chamada.args.get("toolConfirmation") or {}
                pendentes.append(
                    {
                        "id": chamada.id,
                        "acao": original["name"],
                        "detalhes": pedido.get("payload") or original.get("args") or {},
                    }
                )
    return pendentes


async def _executar(session_id: str, conteudo: types.Content) -> dict:
    textos = []
    async for evento in runner.run_async(user_id=USER_ID, session_id=session_id, new_message=conteudo):
        if evento.author != "user" and evento.content and evento.content.parts and not evento.partial:
            textos += [p.text for p in evento.content.parts if p.text and not p.thought]
    sessao = await _sessao(session_id)
    return {"resposta": "\n".join(t.strip() for t in textos if t.strip()), "confirmacoes_pendentes": _pendentes(sessao)}


@app.post("/sessoes", status_code=201)
async def criar_sessao(corpo: NovaSessao):
    if corpo.apartamento not in APARTAMENTOS:
        raise HTTPException(422, "Apartamento inexistente.")
    # Garantia 2: o apartamento entra no state uma única vez, aqui; as tools só leem daqui.
    sessao = await sessoes.create_session(
        app_name=APP_NAME, user_id=USER_ID, state={"apartamento": corpo.apartamento}
    )
    return {"session_id": sessao.id}


@app.post("/sessoes/{session_id}/mensagens")
async def enviar_mensagem(session_id: str, corpo: Mensagem):
    await _sessao(session_id)
    return await _executar(session_id, types.Content(role="user", parts=[types.Part(text=corpo.texto)]))


@app.post("/sessoes/{session_id}/confirmacoes")
async def responder_confirmacao(session_id: str, corpo: Resposta):
    # Garantia 1: só aceita id que está pendente nesta sessão; o resto é 409 e nada roda.
    if corpo.id not in {p["id"] for p in _pendentes(await _sessao(session_id))}:
        raise HTTPException(409, "Não existe confirmação pendente com esse id nesta sessão.")
    resposta = types.FunctionResponse(id=corpo.id, name=CONFIRMACAO, response={"confirmed": corpo.confirmado})
    return await _executar(session_id, types.Content(role="user", parts=[types.Part(function_response=resposta)]))


@app.get("/sessoes/{session_id}/eventos")
async def listar_eventos(session_id: str):
    sessao = await _sessao(session_id)
    return [e.model_dump(mode="json", by_alias=True, exclude_none=True) for e in sessao.events]


@app.get("/apartamentos/{numero}/reservas")
def reservas(numero: str):
    return db.reservas_do_apartamento(numero)


@app.get("/apartamentos/{numero}/visitantes")
def visitantes(numero: str):
    return db.visitantes_do_apartamento(numero)
