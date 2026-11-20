"""Dados do condomínio em SQLite. `python -m aurora.db` restaura o estado inicial de dados/."""

import json
import sqlite3
import uuid
from contextlib import closing
from pathlib import Path

RAIZ = Path(__file__).resolve().parent.parent
DADOS = RAIZ / "dados"
ESTADO = RAIZ / "estado"
DB = ESTADO / "aurora.db"

AREAS = {a["id"]: a for a in json.loads((DADOS / "areas.json").read_text())}

SCHEMA = """
CREATE TABLE IF NOT EXISTS reservas (
  codigo TEXT PRIMARY KEY,           -- PK: código nunca se repete, nem de reserva cancelada
  apartamento TEXT NOT NULL,
  area TEXT NOT NULL,
  data TEXT NOT NULL,
  ativa INTEGER NOT NULL DEFAULT 1   -- cancelar = ativa 0; a linha fica para o código não voltar
);
-- Garantia 5: no máximo uma reserva ATIVA por área e data, conferido pelo SQLite no INSERT.
CREATE UNIQUE INDEX IF NOT EXISTS uma_reserva_ativa_por_area_data
  ON reservas (area, data) WHERE ativa = 1;
CREATE TABLE IF NOT EXISTS visitantes (
  id INTEGER PRIMARY KEY,
  apartamento TEXT NOT NULL,
  nome TEXT NOT NULL,
  data TEXT NOT NULL
);
"""


def conectar() -> sqlite3.Connection:
    ESTADO.mkdir(exist_ok=True)
    con = sqlite3.connect(DB, timeout=30)
    con.execute("PRAGMA journal_mode=WAL")
    return con


def iniciar() -> None:
    """Cria as tabelas; num banco novo carrega os dados iniciais."""
    with closing(conectar()) as con, con:
        con.executescript(SCHEMA)
        vazio = con.execute("SELECT COUNT(*) FROM reservas").fetchone()[0] == 0
    if vazio:
        restaurar()


def restaurar() -> None:
    """Volta reservas e visitantes ao estado de dados/*.json (sessões não são tocadas)."""
    reservas = json.loads((DADOS / "reservas.json").read_text())
    visitantes = json.loads((DADOS / "visitantes.json").read_text())
    with closing(conectar()) as con, con:
        con.executescript(SCHEMA)
        con.execute("DELETE FROM reservas")
        con.execute("DELETE FROM visitantes")
        con.executemany(
            "INSERT INTO reservas (codigo, apartamento, area, data) VALUES (:codigo, :apartamento, :area, :data)",
            reservas,
        )
        con.executemany(
            "INSERT INTO visitantes (apartamento, nome, data) VALUES (:apartamento, :nome, :data)",
            visitantes,
        )


def reservas_do_apartamento(apartamento: str) -> list[dict]:
    with closing(conectar()) as con:
        linhas = con.execute(
            "SELECT codigo, area, data FROM reservas WHERE apartamento = ? AND ativa = 1 ORDER BY data, codigo",
            (apartamento,),
        ).fetchall()
    return [{"codigo": c, "area": a, "data": d} for c, a, d in linhas]


def visitantes_do_apartamento(apartamento: str) -> list[dict]:
    with closing(conectar()) as con:
        linhas = con.execute(
            "SELECT nome, data FROM visitantes WHERE apartamento = ? ORDER BY data, id", (apartamento,)
        ).fetchall()
    return [{"nome": n, "data": d} for n, d in linhas]


def data_livre(area: str, data: str) -> bool:
    """Só diz se está livre; nunca de quem é a reserva."""
    with closing(conectar()) as con:
        return con.execute(
            "SELECT 1 FROM reservas WHERE area = ? AND data = ? AND ativa = 1", (area, data)
        ).fetchone() is None


def criar_reserva(apartamento: str, area: str, data: str) -> str | None:
    """Grava a reserva e devolve o código, ou None se a área já estiver reservada na data.

    A exclusividade não depende de conferência prévia: o índice único parcial faz o
    INSERT concorrente perdedor falhar com IntegrityError no instante da gravação.
    """
    with closing(conectar()) as con:
        while True:
            codigo = f"RSV-{uuid.uuid4().hex[:8].upper()}"
            try:
                with con:
                    con.execute(
                        "INSERT INTO reservas (codigo, apartamento, area, data) VALUES (?, ?, ?, ?)",
                        (codigo, apartamento, area, data),
                    )
                return codigo
            except sqlite3.IntegrityError as erro:
                if "reservas.codigo" in str(erro):
                    continue  # colisão de código (improvável): sorteia outro
                return None  # índice único de área+data: outra reserva ativa venceu


def cancelar_reserva(apartamento: str, codigo: str) -> bool:
    """Cancela só se a reserva for do apartamento informado (o da sessão)."""
    with closing(conectar()) as con, con:
        cur = con.execute(
            "UPDATE reservas SET ativa = 0 WHERE codigo = ? AND apartamento = ? AND ativa = 1",
            (codigo.strip().upper(), apartamento),
        )
        return cur.rowcount == 1


def autorizar_visitante(apartamento: str, nome: str, data: str) -> None:
    with closing(conectar()) as con, con:
        con.execute(
            "INSERT INTO visitantes (apartamento, nome, data) VALUES (?, ?, ?)", (apartamento, nome, data)
        )


if __name__ == "__main__":
    restaurar()
    print(f"Dados restaurados em {DB}")
