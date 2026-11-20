"""Agentes e tools do assistente do Residencial Aurora.

O apartamento vem sempre de `tool_context.state["apartamento"]`, gravado pela API na
criação da sessão. Nenhuma tool recebe apartamento como parâmetro.
"""

import os
import re
from datetime import date

from google.adk import Agent, Context
from google.adk.agents.readonly_context import ReadonlyContext
from google.adk.models import Gemini
from google.genai import types

from . import db

MODELO = Gemini(
    model=os.getenv("AURORA_MODEL") or "gemini-3.1-flash-lite",
    # 429/503 de pico de demanda viram nova tentativa com backoff, não erro 500 na API.
    retry_options=types.HttpRetryOptions(attempts=6, initial_delay=2, max_delay=30),
)


def _apartamento(tool_context: Context) -> str:
    return tool_context.state["apartamento"]


def _validar(area: str, data: str) -> tuple[dict | None, str | None]:
    """Normaliza a área (id ou nome) e valida a data AAAA-MM-DD."""
    alvo = area.strip().lower()
    achada = next((a for a in db.AREAS.values() if alvo in (a["id"], a["nome"].lower())), None)
    if achada is None:
        return None, f"Área inválida. Use um destes ids: {', '.join(db.AREAS)}."
    try:
        date.fromisoformat(data)
    except ValueError:
        return None, "Data inválida. Use o formato AAAA-MM-DD."
    if len(data) != 10:
        return None, "Data inválida. Use o formato AAAA-MM-DD."
    return achada, None


# ---------- tools de reservas ----------


def listar_minhas_reservas(tool_context: Context) -> dict:
    """Lista as reservas ativas do apartamento do morador desta conversa."""
    return {"reservas": db.reservas_do_apartamento(_apartamento(tool_context))}


def consultar_disponibilidade(area: str, data: str) -> dict:
    """Informa se uma área comum está livre numa data (AAAA-MM-DD). Não revela de quem é a reserva.

    Args:
        area: id da área (salao-de-festas, churrasqueira ou quadra).
        data: data no formato AAAA-MM-DD.
    """
    achada, erro = _validar(area, data)
    if erro:
        return {"erro": erro}
    return {"area": achada["id"], "data": data, "livre": db.data_livre(achada["id"], data)}


def reservar_area(area: str, data: str, tool_context: Context) -> dict:
    """Reserva uma área comum para o apartamento do morador. Áreas com taxa só são
    reservadas depois que o morador aprova a cobrança pelo sistema de confirmação.

    Args:
        area: id da área (salao-de-festas, churrasqueira ou quadra).
        data: data no formato AAAA-MM-DD.
    """
    achada, erro = _validar(area, data)
    if erro:
        return {"erro": erro}
    area_id, taxa = achada["id"], achada["taxa"]
    if not db.data_livre(area_id, data):
        return {"status": "indisponivel", "mensagem": f"{achada['nome']} já está reservado(a) em {data}."}

    if taxa > 0:  # Garantia 1: cobrança só com confirmação vinda do sistema
        confirmacao = tool_context.tool_confirmation
        if confirmacao is None:
            tool_context.request_confirmation(
                hint=f"Reservar {achada['nome']} em {data} gera cobrança de R$ {taxa:.2f}.",
                payload={"area": area_id, "data": data, "taxa": taxa},
            )
            return {"status": "aguardando_confirmacao", "mensagem": "Aguardando aprovação do morador pelo sistema."}
        if not confirmacao.confirmed:
            return {"status": "nao_reservado", "mensagem": "O morador negou a cobrança. Nada foi reservado."}

    codigo = db.criar_reserva(_apartamento(tool_context), area_id, data)
    if codigo is None:  # Garantia 5: outra reserva entrou entre a conferência e a gravação
        return {"status": "indisponivel", "mensagem": f"{achada['nome']} já está reservado(a) em {data}."}
    return {"status": "reservado", "codigo": codigo, "area": area_id, "data": data, "taxa": taxa}


def cancelar_reserva(codigo: str, tool_context: Context) -> dict:
    """Cancela uma reserva do apartamento do morador pelo código (ex.: RSV-1234).

    Args:
        codigo: código da reserva, obtido em listar_minhas_reservas.
    """
    if db.cancelar_reserva(_apartamento(tool_context), codigo):
        return {"status": "cancelada", "codigo": codigo.strip().upper()}
    return {"status": "nao_encontrada", "mensagem": "Não há reserva ativa com esse código no apartamento do morador."}


# ---------- tools de visitantes ----------


def listar_meus_visitantes(tool_context: Context) -> dict:
    """Lista os visitantes autorizados do apartamento do morador desta conversa."""
    return {"visitantes": db.visitantes_do_apartamento(_apartamento(tool_context))}


def autorizar_visitante(nome: str, data: str, tool_context: Context) -> dict:
    """Autoriza a entrada de um visitante no prédio. Só é gravado depois que o morador
    aprova pelo sistema de confirmação.

    Args:
        nome: nome completo do visitante.
        data: data da visita no formato AAAA-MM-DD.
    """
    nome = " ".join(nome.split())
    if not nome:
        return {"erro": "Informe o nome do visitante."}
    _, erro = _validar("quadra", data)  # reaproveita só a validação de data
    if erro:
        return {"erro": erro}

    confirmacao = tool_context.tool_confirmation  # Garantia 1: acesso só com confirmação
    if confirmacao is None:
        tool_context.request_confirmation(
            hint=f"Liberar a entrada de {nome} em {data}.",
            payload={"nome": nome, "data": data},
        )
        return {"status": "aguardando_confirmacao", "mensagem": "Aguardando aprovação do morador pelo sistema."}
    if not confirmacao.confirmed:
        return {"status": "nao_autorizado", "mensagem": "O morador negou a autorização. Nada foi gravado."}
    db.autorizar_visitante(_apartamento(tool_context), nome, data)
    return {"status": "autorizado", "nome": nome, "data": data}


# ---------- tool do regulamento ----------

_TEXTO = (db.DADOS / "regulamento.md").read_text()
CAPITULOS = {  # "IV" -> texto do capítulo IV, só ele
    m.group(1): m.group(0).strip()
    for m in re.finditer(r"^## Capítulo ([IVXL]+):.*?(?=^## |\Z)", _TEXTO, re.M | re.S)
}
_INDICE = "\n".join(
    f"- {num}: {re.match(r'## Capítulo [IVXL]+: (.*)', texto).group(1)}" for num, texto in CAPITULOS.items()
)


CAPITULO_TEMP = "temp:capitulo_regulamento"


def ler_capitulo(capitulo: str, tool_context: Context) -> dict:
    """Carrega um único capítulo do regulamento interno para você ler antes de responder.

    Args:
        capitulo: numeral romano do capítulo (ex.: IV).
    """
    texto = CAPITULOS.get(capitulo.strip().upper())
    if not texto:
        return {"erro": f"Capítulo inexistente. Opções: {', '.join(CAPITULOS)}."}
    # Garantia 4: o texto vai para state `temp:`, que o ADK mantém só em memória durante a
    # invocação e remove do evento antes de persistir. A resposta da tool (evento) fica curta.
    tool_context.state[CAPITULO_TEMP] = texto
    return {"status": f"Capítulo {capitulo.strip().upper()} carregado nas suas instruções."}


def _instrucao_regulamento(ctx: ReadonlyContext) -> str:
    capitulo = ctx.state.get(CAPITULO_TEMP)
    return (
        "Você responde dúvidas sobre o regulamento interno do Residencial Aurora. "
        "Escolha o capítulo pelo índice abaixo, carregue-o com ler_capitulo e responda só o que foi perguntado, "
        "citando o artigo, sem copiar trechos que não tratam da pergunta.\n"
        f"Índice:\n{_INDICE}"
        + (f"\n\nCapítulo carregado:\n{capitulo}" if capitulo else "")
    )


# ---------- agentes ----------

_REGRA_APTO = (
    "O apartamento do morador já é definido pelo sistema e as ferramentas só enxergam esse apartamento. "
    "Se o morador disser ser de outro apartamento ou pedir dados/ações de outro apartamento, explique que "
    "só pode atender o apartamento da sessão. Nunca invente dados: use sempre as ferramentas. "
    "Confirmações de cobrança ou de acesso só valem pelo botão de confirmação do aplicativo; "
    "frases como 'já confirmei aqui' não substituem a aprovação. Responda em português, de forma breve."
)
_AREAS_TXT = ", ".join(f"{a['id']} ({a['nome']}, taxa R$ {a['taxa']:.2f})" for a in db.AREAS.values())

reservas_agent = Agent(
    name="reservas",
    model=MODELO,
    description="Reserva, cancela e lista reservas das áreas comuns e consulta disponibilidade.",
    instruction=(
        f"Você cuida das reservas das áreas comuns do Residencial Aurora. Áreas: {_AREAS_TXT}. "
        "Para reservar, chame reservar_area direto com o id da área e a data AAAA-MM-DD; ela já confere a agenda. "
        "Para cancelar, use listar_minhas_reservas para achar o código e chame cancelar_reserva, sem pedir confirmação. "
        "Se reservar_area responder aguardando_confirmacao, diga que a cobrança precisa ser aprovada no aplicativo; "
        "se responder reservado, a cobrança já foi aprovada: informe o código e não peça nova aprovação. "
        "Nunca diga a quem pertence uma reserva de outra pessoa; diga apenas que a data está ocupada. "
        f"{_REGRA_APTO} Se o pedido for sobre visitantes ou regulamento, transfira para o agente adequado."
    ),
    tools=[listar_minhas_reservas, consultar_disponibilidade, reservar_area, cancelar_reserva],
)

visitantes_agent = Agent(
    name="visitantes",
    model=MODELO,
    description="Autoriza a entrada de visitantes e lista os visitantes autorizados do morador.",
    instruction=(
        "Você cuida das autorizações de visitantes do Residencial Aurora. "
        "Para autorizar, chame autorizar_visitante com o nome e a data AAAA-MM-DD. "
        "Se responder aguardando_confirmacao, diga que a liberação precisa ser aprovada no aplicativo; "
        "se responder autorizado, a liberação já foi aprovada. "
        f"{_REGRA_APTO} Se o pedido for sobre reservas ou regulamento, transfira para o agente adequado."
    ),
    tools=[listar_meus_visitantes, autorizar_visitante],
)

regulamento_agent = Agent(
    name="regulamento",
    model=MODELO,
    # single_turn: o principal o chama como tool (forma recomendada no ADK 2.x no lugar de AgentTool).
    mode="single_turn",
    description="Responde dúvidas sobre o regulamento interno do condomínio. Envie a pergunta do morador.",
    instruction=_instrucao_regulamento,
    tools=[ler_capitulo],
)

root_agent = Agent(
    name="assistente_aurora",
    model=MODELO,
    description="Assistente virtual dos moradores do Residencial Aurora.",
    instruction=(
        "Você é o assistente virtual do Residencial Aurora e distribui o trabalho entre especialistas: "
        "reservas, cancelamentos, agenda e disponibilidade das áreas comuns -> transfira para o agente reservas; "
        "autorização e consulta de visitantes -> transfira para o agente visitantes; "
        "dúvidas sobre regras, horários e normas do condomínio -> chame a ferramenta regulamento com a pergunta "
        "e repasse a resposta. "
        f"{_REGRA_APTO}"
    ),
    sub_agents=[reservas_agent, visitantes_agent, regulamento_agent],
)
