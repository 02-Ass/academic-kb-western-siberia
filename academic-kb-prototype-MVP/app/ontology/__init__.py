"""Онтология нефтегазовых объектов и публикаций с логическим выводом (OWL 2 RL).
Строится из реляционной базы; см. vocab.py (аксиомы), reasoner.py (вывод и кэш), queries.py (запросы)."""
from . import mapping, queries, tbox, vocab
from .reasoner import OntologyState, get_state, get_state_nowait, invalidate

__all__ = ["mapping", "queries", "tbox", "vocab", "OntologyState", "get_state", "get_state_nowait", "invalidate"]
