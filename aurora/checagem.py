"""Checagem das regras gravadas no banco, sem modelo: `uv run python -m aurora.checagem`."""

import tempfile
import threading
from pathlib import Path

from . import db

db.DB = Path(tempfile.mkdtemp()) / "checagem.db"  # nunca toca o banco real
db.restaurar()

# Garantia 5: 20 gravações simultâneas, sem conferência prévia, na mesma área/data -> só uma vence.
codigos: list[str | None] = []
barreira = threading.Barrier(20)


def gravar(apto: str) -> None:
    barreira.wait()
    codigos.append(db.criar_reserva(apto, "churrasqueira", "2099-01-01"))


threads = [threading.Thread(target=gravar, args=(f"{i:03d}",)) for i in range(20)]
[t.start() for t in threads]
[t.join() for t in threads]
vencedor = [c for c in codigos if c]
assert len(vencedor) == 1, codigos

# Garantia 2: cancelar exige que a reserva seja do apartamento informado.
assert not db.cancelar_reserva("101", "RSV-4821")
assert any(r["codigo"] == "RSV-4821" for r in db.reservas_do_apartamento("302"))

# Regra 5: após cancelar, a data volta a ficar livre e o novo código não repete o cancelado.
dono = next(f"{i:03d}" for i in range(20) if db.reservas_do_apartamento(f"{i:03d}"))
assert db.cancelar_reserva(dono, vencedor[0]) and db.data_livre("churrasqueira", "2099-01-01")
novo = db.criar_reserva("101", "churrasqueira", "2099-01-01")
assert novo and novo != vencedor[0]
# Mesmo dia escrito em outro formato não fura a exclusividade.
assert db.criar_reserva("101", "salao-de-festas", "2030-W11-6") is None  # CHECK do banco recusa
from .agent import _validar  # noqa: E402

assert _validar("salao-de-festas", "2030-W11-6")[1] == "2030-03-16"
assert _validar("salao-de-festas", "20300316")[1] == "2030-03-16"
assert not db.data_livre("salao-de-festas", _validar("salao-de-festas", "2030-W11-6")[1])
print("checagem ok")
